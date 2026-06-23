"""Trading Engine for Dual Grid Strategy - main orchestrator."""

import logging
import pandas as pd
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from collections import deque

from app.trading.grid_models import GridPosition, Direction, ExecutionApproval, DegradationMode
from app.trading.grid_manager import GridManager
from app.trading.portfolio_manager import PortfolioManager
from app.trading.atr_calculator import compute_atr, compute_atr_baseline
from app.trading.volume import (
    lot_to_volume_cents,
    volume_cents_to_lot,
    resolve_order_volume,
)
from app.config.settings import config
from app.scanner.screener_runtime import MultiSetupScreener
from app.scanner.types.enums import Direction as ScannerDirection


logger = logging.getLogger(__name__)


class RollingWindow:
    """Simple rolling window for storing history."""

    def __init__(self, max_size: int):
        self.max_size = max_size
        self.window: deque = deque(maxlen=max_size)

    def add(self, value: float) -> None:
        self.window.append(value)

    def mean(self) -> float:
        if not self.window:
            return 0.0
        return sum(self.window) / len(self.window)

    def is_full(self) -> bool:
        return len(self.window) == self.max_size


def _broker_direction_to_grid(broker_direction: str) -> Direction:
    if str(broker_direction).upper() in ("BUY", "LONG"):
        return Direction.LONG
    return Direction.SHORT


def _positions_by_id(positions: List[dict]) -> Dict[str, dict]:
    return {str(p["id"]): p for p in positions}


def _position_volume_lots(pos_data: dict) -> float:
    return volume_cents_to_lot(int(pos_data.get("volume", 0)))


def _is_watched(pair: str, timeframe: str, watched: List[tuple[str, str]]) -> bool:
    return (pair, timeframe) in watched


def _is_within_trade_window() -> bool:
    now = datetime.now(timezone.utc).time()
    return config.TRADE_WINDOW_START <= now <= config.TRADE_WINDOW_END


def _timestamp_to_ms(ts: Any) -> int:
    """Candle.timestamp may be datetime (main.py) or int ms (API trendbars)."""
    if isinstance(ts, datetime):
        return int(ts.timestamp() * 1000)
    return int(ts)


class TradingEngine:
    """Main trading engine for Dual Grid Strategy."""

    def __init__(self):
        self.watched_instruments = config.WATCHED_INSTRUMENTS

        self.long_grid = GridManager(Direction.LONG)
        self.short_grid = GridManager(Direction.SHORT)

        self.portfolio = PortfolioManager()

        self.spread_history: Dict[str, RollingWindow] = {}
        self.atr_baseline_history: Dict[str, RollingWindow] = {}
        self.pending_order_metadata: Dict[str, Dict[str, Any]] = {}
        self._screener: Optional[MultiSetupScreener] = None
        self._last_degradation_mode: str = DegradationMode.NORMAL

        for symbol, _ in self.watched_instruments:
            self.spread_history[symbol] = RollingWindow(config.SPREAD_LOOKBACK_BARS)
            self.atr_baseline_history[symbol] = RollingWindow(config.ATR_BASELINE_LOOKBACK_BARS)

    def _ensure_screener(self) -> MultiSetupScreener:
        if self._screener is None:
            watched_pairs = [p for p, _ in self.watched_instruments]
            pair_configs = {p: config.get_pair_config(p) for p in watched_pairs}
            self._screener = MultiSetupScreener(pair_configs=pair_configs)
        return self._screener

    def restart(self, equity: float) -> None:
        """Restart after halt - reset state."""
        self.portfolio.restart(equity)
        self.pending_order_metadata.clear()
        self._last_degradation_mode = DegradationMode.NORMAL
        logger.info("TradingEngine: restarted")

    async def on_bar_update(
        self,
        state: Any,
        orchestrator: Any,
        client: Any,
        market_cache: Any,
    ) -> None:
        """Main entry point called on each bar update for a single symbol."""
        pair = state.pair
        entry_tf = state.entry_tf

        if not _is_watched(pair, entry_tf, self.watched_instruments):
            return

        symbol_id = state.symbol_id
        candles = state.candles

        await self._reconcile_positions(client, pair)

        await self._check_pending_orders_ttl(client, pair, candles, state.entry_minutes)

        current_spread = await client.get_spread(pair)
        if current_spread is None:
            current_spread = 0.0
        self.spread_history[pair].add(float(current_spread))

        if self.portfolio.margin_per_lot == 0.0:
            margin = await client.get_expected_margin(
                symbol_id, volume_cents=lot_to_volume_cents(1.0),
            )
            if margin is None:
                logger.critical(f"TradingEngine: get_expected_margin returned None for {pair}")
                return
            self.portfolio.margin_per_lot = margin
            logger.info(f"TradingEngine: cached margin_per_lot={margin:.2f} for {pair}")

        candles_df = self._candles_to_dataframe(candles)
        current_atr = compute_atr(candles_df, config.ATR_PERIOD)
        self.atr_baseline_history[pair].add(current_atr)
        atr_baseline = compute_atr_baseline(list(self.atr_baseline_history[pair].window))

        if self.portfolio.halted:
            logger.debug(f"TradingEngine: halted, skipping {pair}")
            return

        positions = await client.get_positions(force=True)
        total_profit = sum(p.get("profit", 0) for p in positions)
        equity = await client.get_balance()
        if equity is None:
            logger.critical(f"TradingEngine: get_balance returned None for {pair}")
            return

        self.portfolio.initialize_baseline(equity)

        portfolio_pnl_fraction = self.portfolio.calculate_portfolio_pnl_fraction(total_profit)
        if self.portfolio.halted:
            return

        tp_sl_result = self.portfolio.check_global_tp_sl(portfolio_pnl_fraction)

        if tp_sl_result:
            logger.warning(f"TradingEngine: Global {tp_sl_result} hit, closing all positions")
            await self._resolve_pending_before_close(client, pair)
            await self._close_all_positions(client, orchestrator, pair)
            self.portfolio.halted = True
            return

        degradation_mode = self.portfolio.determine_degradation_mode(
            portfolio_pnl_fraction, current_atr, atr_baseline,
        )
        await self._handle_degradation_transition(
            degradation_mode, client, orchestrator, pair,
        )

        if degradation_mode == DegradationMode.EXIT:
            logger.warning("TradingEngine: EXIT mode, closing worst position")
            await self._resolve_pending_before_close(client, pair)
            await self._close_worst_position(client, orchestrator, pair)
            return

        candidates, _context = self._ensure_screener().scan_pair(pair, candles, entry_tf)

        if not candidates:
            return

        candidate = candidates[0]
        scanner_direction = candidate.direction
        if scanner_direction == ScannerDirection.BUY:
            direction = Direction.LONG
        elif scanner_direction == ScannerDirection.SELL:
            direction = Direction.SHORT
        else:
            logger.warning(f"TradingEngine: unknown scanner direction {scanner_direction}")
            return

        await self._process_grid_signal(
            direction=direction,
            score=candidate.score,
            current_price=float(candles_df["close"].iloc[-1]),
            current_atr=current_atr,
            degradation_mode=degradation_mode,
            orchestrator=orchestrator,
            client=client,
            pair=pair,
            symbol_id=symbol_id,
        )

    def _candles_to_dataframe(self, candles: List) -> pd.DataFrame:
        data = []
        for candle in candles:
            data.append({
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            })
        return pd.DataFrame(data)

    async def _reconcile_positions(self, client: Any, pair: str) -> None:
        """Reconcile GridManager positions with broker state."""
        broker_positions = _positions_by_id(await client.get_positions(force=False))
        broker_ids = set(broker_positions.keys())

        for grid_manager in [self.long_grid, self.short_grid]:
            to_remove = []
            for pos in grid_manager.positions:
                if pos.position_id and str(pos.position_id) not in broker_ids:
                    to_remove.append(str(pos.position_id))

            for pos_id in to_remove:
                grid_manager.remove_position(pos_id)
                logger.info(f"TradingEngine: removed stale position {pos_id} from memory")

        spread = await client.get_spread(pair) or 0.0

        for pos_id, pos_data in broker_positions.items():
            if pos_data.get("symbol") != pair:
                continue

            direction = _broker_direction_to_grid(pos_data.get("direction", ""))
            grid_manager = self.long_grid if direction == Direction.LONG else self.short_grid

            if grid_manager.get_position_by_id(pos_id):
                continue

            matched = self._match_pending_order_metadata(pos_data, direction, pair, spread)
            if matched:
                position = GridPosition(
                    direction=direction,
                    entry_price=float(pos_data.get("entry_price", 0)),
                    volume=_position_volume_lots(pos_data),
                    stop_loss=0.0,
                    client_order_id=matched["client_order_id"],
                    position_id=pos_id,
                    order_placed_at=matched["order_placed_at"],
                    position_opened_at=datetime.now(timezone.utc),
                    grid_step_at_open=matched["grid_step_at_open"],
                    level_index=matched["level_index"],
                    signal_score_at_open=matched["signal_score_at_open"],
                )
                grid_manager.add_position(position)
                del self.pending_order_metadata[matched["client_order_id"]]
                logger.info(f"TradingEngine: restored position {pos_id} from pending metadata")
            else:
                logger.warning(f"TradingEngine: unmatched position {pos_id} at broker for {pair}")

    def _match_pending_order_metadata(
        self,
        pos_data: Dict,
        direction: Direction,
        pair: str,
        spread_tolerance: float,
    ) -> Optional[Dict]:
        entry_price = float(pos_data.get("entry_price", 0))
        tolerance = max(spread_tolerance / 2, 1e-8)
        matches = []

        for client_order_id, metadata in self.pending_order_metadata.items():
            if metadata.get("pair") != pair:
                continue
            if metadata["direction"] != direction:
                continue
            price_diff = abs(entry_price - float(metadata.get("entry_price", 0)))
            if price_diff < tolerance:
                matches.append({**metadata, "client_order_id": client_order_id})

        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                f"TradingEngine: multiple pending metadata matches for {pair} "
                f"entry={entry_price} — treating as unmatched",
            )
        return None

    async def _check_pending_orders_ttl(
        self,
        client: Any,
        pair: str,
        candles: List,
        entry_minutes: int,
    ) -> None:
        if not candles:
            return

        current_bar = candles[-1]
        current_bar_ms = _timestamp_to_ms(current_bar.timestamp)
        bar_duration_ms = entry_minutes * 60 * 1000

        to_cancel = []
        for client_order_id, metadata in self.pending_order_metadata.items():
            if metadata.get("pair") != pair:
                continue
            order_placed_ms = _timestamp_to_ms(metadata["order_placed_at"])
            elapsed_ms = current_bar_ms - order_placed_ms
            if elapsed_ms > config.GRID_ORDER_TTL_BARS * bar_duration_ms:
                to_cancel.append(client_order_id)

        for client_order_id in to_cancel:
            try:
                await client.cancel_order_by_client_id(client_order_id, timeout=5)
                self._remove_pending_level(client_order_id)
                logger.info(f"TradingEngine: cancelled expired pending order {client_order_id}")
            except Exception as e:
                logger.error(f"TradingEngine: failed to cancel expired order {client_order_id}: {e}")

    def _remove_pending_level(self, client_order_id: str) -> None:
        self.pending_order_metadata.pop(client_order_id, None)
        for grid_manager in [self.long_grid, self.short_grid]:
            grid_manager.remove_position_by_client_order_id(client_order_id)

    def _effective_add_threshold(self, direction: Direction, degradation_mode: str) -> float:
        threshold = config.ADD_LEVEL_THRESHOLD
        if degradation_mode == DegradationMode.CONSERVATIVE:
            threshold += config.CONSERVATIVE_SCORE_PENALTY

        long_volume = self.long_grid.total_exposure_lots()
        short_volume = self.short_grid.total_exposure_lots()
        total_volume = long_volume + short_volume
        if total_volume > 0:
            imbalance = abs(long_volume - short_volume) / total_volume
            if imbalance >= config.IMBALANCE_SOFT_THRESHOLD:
                if direction == Direction.LONG and long_volume > short_volume:
                    threshold += config.IMBALANCE_SCORE_PENALTY
                elif direction == Direction.SHORT and short_volume > long_volume:
                    threshold += config.IMBALANCE_SCORE_PENALTY
        return threshold

    async def _process_grid_signal(
        self,
        direction: Direction,
        score: float,
        current_price: float,
        current_atr: float,
        degradation_mode: str,
        orchestrator: Any,
        client: Any,
        pair: str,
        symbol_id: int,
    ) -> None:
        if config.TRADING_MODE != "AUTO":
            return
        if not _is_within_trade_window():
            return

        grid_manager = self.long_grid if direction == Direction.LONG else self.short_grid

        if not grid_manager.is_active():
            if score < config.ENTRY_THRESHOLD:
                return
        else:
            threshold = self._effective_add_threshold(direction, degradation_mode)
            if score < threshold:
                return
            if not self._can_add_level(grid_manager, current_price, degradation_mode):
                return

        volume = await self._calculate_position_size(client, pair, current_atr, degradation_mode)
        if volume <= 0:
            return

        equity = await client.get_balance()
        if equity is None:
            return

        if not self.portfolio.can_expand(
            direction,
            self.long_grid,
            self.short_grid,
            equity,
            self.portfolio.margin_per_lot,
            additional_lots=volume,
        ):
            return

        approval = await self._execution_check(pair, client, current_atr)
        if not approval.approved:
            self.portfolio.increment_execution_rejection()
            return

        volume *= approval.volume_multiplier
        if volume <= 0:
            self.portfolio.increment_execution_rejection()
            return

        placed = await self._place_grid_order(
            direction=direction,
            current_price=current_price,
            current_atr=current_atr,
            volume=volume,
            score=score,
            orchestrator=orchestrator,
            client=client,
            pair=pair,
            symbol_id=symbol_id,
            grid_manager=grid_manager,
        )
        if placed:
            self.portfolio.reset_execution_rejections()

    def _can_add_level(
        self,
        grid_manager: GridManager,
        current_price: float,
        degradation_mode: str,
    ) -> bool:
        last_position = grid_manager.get_last_position()
        if not last_position:
            return False

        distance = abs(current_price - last_position.entry_price)
        if distance < last_position.grid_step_at_open:
            return False

        if not grid_manager.can_add_level(config.MAX_GRID_LEVELS):
            return False

        if degradation_mode in (DegradationMode.FREEZE, DegradationMode.EXIT):
            return False

        return True

    async def _calculate_position_size(
        self,
        client: Any,
        pair: str,
        current_atr: float,
        degradation_mode: str,
    ) -> float:
        equity = await client.get_balance()
        if equity is None:
            logger.critical(f"get_balance returned None for {pair}")
            return 0.0

        if self.portfolio.baseline_equity == 0:
            logger.critical("baseline_equity == 0, halting system")
            self.portfolio.halted = True
            return 0.0

        sl_distance_price = current_atr * config.SL_ATR_MULTIPLIER
        if sl_distance_price == 0:
            logger.warning("sl_distance_price == 0, execution rejection")
            self.portfolio.increment_execution_rejection()
            return 0.0

        try:
            pair_info = client.get_pair_info(pair)
            if not pair_info or len(pair_info) <= 3:
                logger.critical(f"get_pair_info failed for {pair}")
                return 0.0
            pip_value = float(pair_info[3])
            min_volume_cents = int(pair_info[4])
            step_volume_cents = int(pair_info[5])
        except Exception as e:
            logger.critical(f"get_pair_info error for {pair}: {e}")
            return 0.0

        if pip_value == 0:
            logger.warning("pip_value == 0, execution rejection")
            self.portfolio.increment_execution_rejection()
            return 0.0

        risk_amount = equity * config.RISK_PER_TRADE
        raw_volume = risk_amount / (sl_distance_price * pip_value)

        if degradation_mode == DegradationMode.CONSERVATIVE:
            raw_volume *= config.CONSERVATIVE_VOLUME_MULTIPLIER

        resolved = resolve_order_volume(raw_volume, min_volume_cents, step_volume_cents)
        volume = resolved.actual_lot
        volume = min(volume, config.MAX_LOT)
        if volume <= 0:
            return 0.0
        return volume

    async def _execution_check(
        self,
        pair: str,
        client: Any,
        current_atr: float,
    ) -> ExecutionApproval:
        spread_window = self.spread_history[pair]
        atr_window = self.atr_baseline_history[pair]

        current_spread = await client.get_spread(pair)
        if current_spread is None:
            current_spread = 0.0

        if not spread_window.is_full() or not atr_window.is_full():
            return ExecutionApproval(approved=True, volume_multiplier=1.0)

        avg_spread = spread_window.mean()
        atr_baseline = atr_window.mean()

        if current_spread > config.SPREAD_REJECT_MULTIPLIER * avg_spread:
            logger.warning(
                f"Execution check failed: spread {current_spread:.2f} > "
                f"{config.SPREAD_REJECT_MULTIPLIER * avg_spread:.2f}",
            )
            return ExecutionApproval(approved=False)

        if current_atr > config.ATR_SPIKE_MULTIPLIER * atr_baseline:
            logger.warning(
                f"Execution check failed: ATR {current_atr:.5f} > "
                f"{config.ATR_SPIKE_MULTIPLIER * atr_baseline:.5f}",
            )
            return ExecutionApproval(approved=False)

        volume_multiplier = 1.0
        if current_spread > config.SPREAD_WARN_MULTIPLIER * avg_spread:
            volume_multiplier *= config.EXECUTION_WARN_VOLUME_REDUCTION
            logger.info(f"Execution warn: spread elevated, volume reduced to {volume_multiplier:.2f}")

        if current_atr > config.ATR_SPIKE_MULTIPLIER * atr_baseline:
            volume_multiplier *= config.EXECUTION_WARN_VOLUME_REDUCTION
            logger.info(f"Execution warn: ATR elevated, volume reduced to {volume_multiplier:.2f}")

        return ExecutionApproval(approved=True, volume_multiplier=volume_multiplier)

    async def _place_grid_order(
        self,
        direction: Direction,
        current_price: float,
        current_atr: float,
        volume: float,
        score: float,
        orchestrator: Any,
        client: Any,
        pair: str,
        symbol_id: int,
        grid_manager: GridManager,
    ) -> bool:
        grid_step = current_atr * config.ATR_MULTIPLIER

        if direction == Direction.LONG:
            stop_loss = current_price - grid_step * config.SL_ATR_MULTIPLIER
            broker_direction = "BUY"
        else:
            stop_loss = current_price + grid_step * config.SL_ATR_MULTIPLIER
            broker_direction = "SELL"

        pair_info = client.get_pair_info(pair)
        if not pair_info:
            logger.critical(f"get_pair_info missing for {pair}")
            return False
        min_volume_cents = int(pair_info[4])
        step_volume_cents = int(pair_info[5])

        pending_orders = await client.get_pending_orders(force=True)
        for order in pending_orders:
            client_order_id = order.get("clientOrderId") or ""
            if not client_order_id or client_order_id not in self.pending_order_metadata:
                continue
            if self.pending_order_metadata[client_order_id].get("pair") != pair:
                continue
            if grid_manager.get_position_by_client_order_id(client_order_id):
                logger.warning(f"Duplicate pending order detected: {client_order_id}, skipping")
                return False
            logger.warning(
                f"Duplicate pending order at broker: {client_order_id}, marking level as open",
            )
            metadata = self.pending_order_metadata[client_order_id]
            position = GridPosition(
                direction=direction,
                entry_price=float(metadata.get("entry_price", current_price)),
                volume=volume,
                stop_loss=stop_loss,
                client_order_id=client_order_id,
                order_placed_at=metadata["order_placed_at"],
                grid_step_at_open=metadata["grid_step_at_open"],
                level_index=metadata["level_index"],
                signal_score_at_open=metadata["signal_score_at_open"],
            )
            grid_manager.add_position(position)
            return False

        result = await client.place_limit_order(
            symbol_id=symbol_id,
            direction=broker_direction,
            lot=volume,
            entry=current_price,
            stop_loss=stop_loss,
            multiplier=1,
            min_volume=min_volume_cents,
            step_volume=step_volume_cents,
            pair=pair,
        )

        if result is None:
            logger.error(f"place_limit_order returned None for {pair}")
            self.portfolio.increment_execution_rejection()
            return False

        client_order_id, resolved_volume = result
        level_index = grid_manager.level_count() + 1
        now = datetime.now(timezone.utc)

        self.pending_order_metadata[client_order_id] = {
            "level_index": level_index,
            "grid_step_at_open": grid_step,
            "signal_score_at_open": score,
            "order_placed_at": now,
            "direction": direction,
            "entry_price": current_price,
            "pair": pair,
        }

        try:
            await orchestrator.track_limit_order(pair, client_order_id)
        except Exception as e:
            logger.critical(f"Failed to track limit order {client_order_id}: {e}")
            return False

        position = GridPosition(
            direction=direction,
            entry_price=current_price,
            volume=resolved_volume.actual_lot,
            stop_loss=stop_loss,
            client_order_id=client_order_id,
            order_placed_at=now,
            grid_step_at_open=grid_step,
            level_index=level_index,
            signal_score_at_open=score,
        )
        grid_manager.add_position(position)

        logger.info(
            f"TradingEngine: placed grid order {client_order_id} for {pair}, "
            f"direction={direction.value}, level={level_index}, "
            f"volume={resolved_volume.actual_lot:.2f}",
        )
        return True

    async def _resolve_pending_before_close(self, client: Any, pair: str) -> None:
        """Resolve position_id=None entries and cancel unresolvable pending orders."""
        spread = await client.get_spread(pair) or 0.0
        broker_positions = await client.get_positions(force=True)

        for grid_manager in [self.long_grid, self.short_grid]:
            for pos in list(grid_manager.positions):
                if pos.position_id is not None:
                    continue
                matched_broker = None
                for bp in broker_positions:
                    if bp.get("symbol") != pair:
                        continue
                    if _broker_direction_to_grid(bp.get("direction", "")) != pos.direction:
                        continue
                    tolerance = max(spread / 2, 1e-8)
                    if abs(float(bp.get("entry_price", 0)) - pos.entry_price) < tolerance:
                        matched_broker = bp
                        break
                if matched_broker:
                    pos.position_id = str(matched_broker["id"])
                    pos.position_opened_at = datetime.now(timezone.utc)
                    self.pending_order_metadata.pop(pos.client_order_id, None)
                else:
                    logger.info(
                        f"TradingEngine: cancelling unresolved pending {pos.client_order_id}",
                    )
                    try:
                        await client.cancel_order_by_client_id(pos.client_order_id, timeout=5)
                    except Exception as e:
                        logger.error(f"Failed to cancel pending {pos.client_order_id}: {e}")
                    self._remove_pending_level(pos.client_order_id)

    async def _close_position_with_verify(
        self,
        client: Any,
        position_id: str,
        volume_cents: int,
    ) -> bool:
        try:
            await client.close_position_partial(int(position_id), volume_cents, timeout=10)
        except Exception as e:
            logger.error(f"TradingEngine: failed to close position {position_id}: {e}")
            return False

        positions_after = _positions_by_id(await client.get_positions(force=True))
        if position_id not in positions_after:
            return True

        remaining = positions_after[position_id]
        if int(remaining.get("volume", 0)) == 0:
            return True

        logger.critical(
            f"TradingEngine: position {position_id} still open after close, retrying once",
        )
        try:
            await client.close_position_partial(
                int(position_id), int(remaining.get("volume", 0)), timeout=10,
            )
        except Exception as e:
            logger.critical(f"TradingEngine: retry close failed for {position_id}: {e}")
            return False

        positions_final = _positions_by_id(await client.get_positions(force=True))
        if position_id in positions_final and int(positions_final[position_id].get("volume", 0)) > 0:
            logger.critical(f"TradingEngine: position {position_id} still exists after retry")
            return False
        return True

    async def _close_all_positions(self, client: Any, orchestrator: Any, pair: str) -> None:
        await self._cancel_all_pending_orders(client, orchestrator, pair)

        positions = await client.get_positions(force=True)
        for pos_data in positions:
            if pos_data.get("symbol") != pair:
                continue
            pos_id = str(pos_data["id"])
            volume_cents = int(pos_data.get("volume", 0))
            ok = await self._close_position_with_verify(client, pos_id, volume_cents)
            if ok:
                logger.info(f"TradingEngine: closed position {pos_id}")
                for gm in [self.long_grid, self.short_grid]:
                    gm.remove_position(pos_id)

        positions_after = await client.get_positions(force=True)
        remaining = [p for p in positions_after if p.get("symbol") == pair]
        if remaining:
            logger.critical(
                f"TradingEngine: positions still exist after close_all for {pair}: "
                f"{[p['id'] for p in remaining]}",
            )
            self.portfolio.halted = True

    async def _close_worst_position(self, client: Any, orchestrator: Any, pair: str) -> None:
        positions = [p for p in await client.get_positions(force=True) if p.get("symbol") == pair]
        if not positions:
            return

        worst = min(positions, key=lambda p: p.get("profit", 0))
        pos_id = str(worst["id"])
        volume_cents = int(worst.get("volume", 0))

        ok = await self._close_position_with_verify(client, pos_id, volume_cents)
        if ok:
            logger.info(f"TradingEngine: closed worst position {pos_id}")
            for gm in [self.long_grid, self.short_grid]:
                gm.remove_position(pos_id)
        else:
            self.portfolio.halted = True

    async def _handle_degradation_transition(
        self,
        degradation_mode: str,
        client: Any,
        orchestrator: Any,
        pair: str,
    ) -> None:
        freeze_or_exit = {DegradationMode.FREEZE, DegradationMode.EXIT}
        prev = self._last_degradation_mode
        self._last_degradation_mode = degradation_mode

        if degradation_mode in freeze_or_exit and prev not in freeze_or_exit:
            await self._cancel_all_pending_orders(client, orchestrator, pair)

    async def _cancel_all_pending_orders(self, client: Any, orchestrator: Any, pair: str) -> None:
        broker_pending = await client.get_pending_orders(force=True)
        broker_ids = {
            o.get("clientOrderId")
            for o in broker_pending
            if o.get("clientOrderId")
        }

        for client_order_id, metadata in list(self.pending_order_metadata.items()):
            if metadata.get("pair") != pair:
                continue

            cancelled = False
            try:
                cancelled = await orchestrator.cancel_tracked_limit_order(pair)
            except Exception:
                cancelled = False

            if not cancelled:
                try:
                    if client_order_id in broker_ids:
                        await client.cancel_order_by_client_id(client_order_id, timeout=5)
                    logger.warning(
                        f"TradingEngine: cancelled untracked order {client_order_id}",
                    )
                except Exception as e:
                    logger.error(f"TradingEngine: failed to cancel order {client_order_id}: {e}")

            self._remove_pending_level(client_order_id)
