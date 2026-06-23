"""Trading Engine for Dual Grid Strategy - main orchestrator."""

import logging
import pandas as pd
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Set
from collections import deque

from app.trading.grid_models import GridPosition, Direction, ExecutionApproval, DegradationMode
from app.trading.grid_manager import GridManager
from app.trading.grid_book import GridBook, PairGrids
from app.trading.portfolio_manager import PortfolioManager
from app.trading.atr_calculator import compute_atr, compute_atr_baseline
from app.trading.volume import (
    lot_to_volume_cents,
    volume_cents_to_lot,
    resolve_order_volume,
    relative_stop_loss_distance,
    format_volume,
    VOLUME_CENTS_PER_LOT,
    PIP_LOT_UNITS,
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


def _position_volume_lots(pos_data: dict, lot_size_cents: int = VOLUME_CENTS_PER_LOT) -> float:
    return volume_cents_to_lot(int(pos_data.get("volume", 0)), lot_size_cents)


def _pair_lot_size_cents(pair_info: tuple) -> int:
    if pair_info and len(pair_info) > 6:
        return int(pair_info[6]) or VOLUME_CENTS_PER_LOT
    return VOLUME_CENTS_PER_LOT


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
        watched_pairs = sorted({p for p, _ in self.watched_instruments})
        self.grids = GridBook(watched_pairs)

        self.portfolio = PortfolioManager()

        self.spread_history: Dict[str, RollingWindow] = {}
        self.atr_baseline_history: Dict[str, RollingWindow] = {}
        self.pending_order_metadata: Dict[str, Dict[str, Any]] = {}
        self._screener: Optional[MultiSetupScreener] = None
        self._last_degradation_mode: str = DegradationMode.NORMAL
        self._margin_per_lot_by_pair: Dict[str, float] = {}

        for symbol, _ in self.watched_instruments:
            self.spread_history[symbol] = RollingWindow(config.SPREAD_LOOKBACK_BARS)
            self.atr_baseline_history[symbol] = RollingWindow(config.ATR_BASELINE_LOOKBACK_BARS)

    def _ensure_screener(self) -> MultiSetupScreener:
        if self._screener is None:
            # Конфиги для всех PAIRS (прогрев/скан), торговля — только WATCHED_INSTRUMENTS.
            pair_configs = {p: config.get_pair_config(p) for p in config.PAIRS}
            self._screener = MultiSetupScreener(pair_configs=pair_configs)
        return self._screener

    def restart(self, equity: float) -> None:
        """Restart after halt - reset state."""
        self.portfolio.restart(equity)
        self.pending_order_metadata.clear()
        self.grids.clear_all()
        self._last_degradation_mode = DegradationMode.NORMAL
        self._margin_per_lot_by_pair.clear()
        logger.info("TradingEngine: restarted")

    def _watched_pairs(self) -> Set[str]:
        return set(self.grids.pairs())

    def on_order_filled(
        self,
        client_order_id: Optional[str],
        position_id: str,
        pair: str,
    ) -> None:
        """Обновить GridPosition после ORDER_FILLED (client callback)."""
        if not client_order_id:
            return
        found = self.grids.find_position_by_client_order_id(client_order_id)
        if found is None:
            return
        _pg, pos = found
        pos.position_id = position_id
        pos.position_opened_at = datetime.now(timezone.utc)
        self.pending_order_metadata.pop(client_order_id, None)
        logger.info(
            f"TradingEngine: [{pair}] order {client_order_id} filled → position {position_id}",
        )

    async def bootstrap_from_broker(self, client: Any, orchestrator: Any) -> None:
        """Восстановить позиции и pending-лимитки с брокера после перезапуска."""
        watched = self._watched_pairs()
        if not watched:
            return

        positions = await client.get_positions(force=True)
        pending_orders = await client.get_pending_orders(force=True)
        restored_positions = 0
        restored_pending = 0

        for pos_data in positions:
            pair = pos_data.get("symbol", "")
            if pair not in watched:
                continue
            if self._restore_broker_position(client, pos_data, source="bootstrap"):
                restored_positions += 1

        for order in pending_orders:
            pair = order.get("pair", "")
            if pair not in watched:
                continue
            if await self._restore_broker_pending(client, orchestrator, order):
                restored_pending += 1

        if restored_positions or restored_pending:
            logger.info(
                f"TradingEngine: bootstrap restored {restored_positions} position(s), "
                f"{restored_pending} pending order(s) from broker",
            )
        else:
            logger.info("TradingEngine: bootstrap — нет открытых позиций/лимиток у брокера")

    def _restore_broker_position(
        self,
        client: Any,
        pos_data: dict,
        *,
        source: str,
        spread: float = 0.0,
    ) -> bool:
        pair = pos_data.get("symbol", "")
        pos_id = str(pos_data.get("id", ""))
        if not pair or not pos_id:
            return False

        direction = _broker_direction_to_grid(pos_data.get("direction", ""))
        if pair not in self.grids.pairs():
            return False
        pair_grids = self.grids.get(pair)
        grid_manager = pair_grids.grid_for(direction)

        if grid_manager.get_position_by_id(pos_id):
            return False

        entry_price = float(pos_data.get("entry_price", 0))
        tolerance = max(spread / 2, entry_price * 1e-6, 1e-4)

        for pos in grid_manager.positions:
            if pos.position_id is None and abs(pos.entry_price - entry_price) < tolerance:
                pos.position_id = pos_id
                pos.position_opened_at = datetime.now(timezone.utc)
                self.pending_order_metadata.pop(pos.client_order_id, None)
                logger.info(
                    f"TradingEngine: linked broker position {pos_id} to pending "
                    f"{pos.client_order_id} [{pair}]",
                )
                return True

        pair_info = client.get_pair_info(pair) if hasattr(client, "get_pair_info") else None
        lot_size_cents = _pair_lot_size_cents(pair_info) if pair_info else VOLUME_CENTS_PER_LOT

        matched = self._match_pending_order_metadata(pos_data, direction, pair, spread)
        if matched:
            position = GridPosition(
                direction=direction,
                entry_price=entry_price,
                volume=_position_volume_lots(pos_data, lot_size_cents),
                stop_loss=0.0,
                client_order_id=matched["client_order_id"],
                pair=pair,
                position_id=pos_id,
                order_placed_at=matched["order_placed_at"],
                position_opened_at=datetime.now(timezone.utc),
                grid_step_at_open=matched["grid_step_at_open"],
                level_index=matched["level_index"],
                signal_score_at_open=matched["signal_score_at_open"],
            )
            grid_manager.add_position(position)
            del self.pending_order_metadata[matched["client_order_id"]]
            logger.info(
                f"TradingEngine: restored position {pos_id} from pending metadata [{pair}]",
            )
            return True

        client_order_id = f"restored-{pos_id}"
        if grid_manager.get_position_by_client_order_id(client_order_id):
            return False

        position = GridPosition(
            direction=direction,
            entry_price=entry_price,
            volume=_position_volume_lots(pos_data, lot_size_cents),
            stop_loss=0.0,
            client_order_id=client_order_id,
            pair=pair,
            position_id=pos_id,
            order_placed_at=datetime.now(timezone.utc),
            position_opened_at=datetime.now(timezone.utc),
            grid_step_at_open=0.0,
            level_index=grid_manager.level_count() + 1,
            signal_score_at_open=0.0,
        )
        grid_manager.add_position(position)
        logger.info(
            f"TradingEngine: restored open position {pos_id} [{pair}] "
            f"{direction.value} ({source})",
        )
        return True

    async def _restore_broker_pending(
        self,
        client: Any,
        orchestrator: Any,
        order: dict,
    ) -> bool:
        pair = order.get("pair", "")
        client_order_id = order.get("clientOrderId") or ""
        if not pair:
            return False
        if not client_order_id:
            client_order_id = f"restored-pending-{order.get('orderId', 0)}"

        direction = _broker_direction_to_grid(order.get("direction", ""))
        if direction not in (Direction.LONG, Direction.SHORT):
            return False
        if pair not in self.grids.pairs():
            return False

        pair_grids = self.grids.get(pair)
        grid_manager = pair_grids.grid_for(direction)
        if grid_manager.get_position_by_client_order_id(client_order_id):
            return False
        if pair_grids.is_active(direction):
            return False

        pair_info = client.get_pair_info(pair) if hasattr(client, "get_pair_info") else None
        lot_size_cents = _pair_lot_size_cents(pair_info) if pair_info else VOLUME_CENTS_PER_LOT
        volume_cents = int(order.get("volume", 0) or 0)
        entry_price = float(order.get("limitPrice", 0) or 0)
        open_ms = int(order.get("open_timestamp_ms", 0) or 0)
        placed_at = (
            datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
            if open_ms > 0
            else datetime.now(timezone.utc)
        )

        level_index = grid_manager.level_count() + 1
        self.pending_order_metadata[client_order_id] = {
            "level_index": level_index,
            "grid_step_at_open": 0.0,
            "signal_score_at_open": 0.0,
            "order_placed_at": placed_at,
            "direction": direction,
            "entry_price": entry_price,
            "pair": pair,
            "restored": True,
        }

        position = GridPosition(
            direction=direction,
            entry_price=entry_price,
            volume=_position_volume_lots({"volume": volume_cents}, lot_size_cents),
            stop_loss=0.0,
            client_order_id=client_order_id,
            pair=pair,
            order_placed_at=placed_at,
            grid_step_at_open=0.0,
            level_index=level_index,
            signal_score_at_open=0.0,
        )
        grid_manager.add_position(position)

        broker_order_id = int(order.get("orderId", 0) or 0)
        if broker_order_id and hasattr(client, "register_order_mapping"):
            client.register_order_mapping(client_order_id, broker_order_id)

        try:
            await orchestrator.track_limit_order(pair, client_order_id)
        except Exception as e:
            logger.warning(f"TradingEngine: track_limit_order on restore failed: {e}")

        logger.info(
            f"TradingEngine: restored pending limit {client_order_id} [{pair}] "
            f"{direction.value} @ {entry_price}",
        )
        return True

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
            logger.debug(
                f"TradingEngine: skip {pair}:{entry_tf} — not in WATCHED_INSTRUMENTS",
            )
            return

        symbol_id = state.symbol_id
        candles = state.candles

        if self.portfolio.halted:
            logger.debug(f"TradingEngine: halted, skipping {pair}")
            return

        await self._reconcile_positions(client, pair)

        await self._check_pending_orders_ttl(client, pair, candles, state.entry_minutes)

        current_spread = await client.get_spread(pair)
        if current_spread is None:
            current_spread = 0.0
        self.spread_history[pair].add(float(current_spread))

        if pair not in self._margin_per_lot_by_pair:
            pair_info = client.get_pair_info(pair)
            lot_size_cents = _pair_lot_size_cents(pair_info) if pair_info else VOLUME_CENTS_PER_LOT
            margin = await client.get_expected_margin(
                symbol_id, volume_cents=lot_size_cents,
            )
            if margin is None:
                logger.critical(f"TradingEngine: get_expected_margin returned None for {pair}")
                return
            self._margin_per_lot_by_pair[pair] = margin
            self.portfolio.margin_per_lot = margin
            logger.info(
                f"TradingEngine: margin 1 lot [{pair}] = {margin:.2f} "
                f"({format_volume(lot_size_cents, lot_size_cents)})",
            )
        margin_per_lot = self._margin_per_lot_by_pair[pair]

        candles_df = self._candles_to_dataframe(candles)
        current_atr = compute_atr(candles_df, config.ATR_PERIOD)
        self.atr_baseline_history[pair].add(current_atr)
        atr_baseline = compute_atr_baseline(list(self.atr_baseline_history[pair].window))

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
            await self._resolve_pending_before_close_global(client)
            await self._close_all_positions_global(client, orchestrator)
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
            await self._resolve_pending_before_close_global(client)
            await self._close_worst_position_global(client, orchestrator)
            return

        candidates, _context = self._ensure_screener().scan_pair(pair, candles, entry_tf)

        if not candidates:
            logger.debug(f"TradingEngine: no setups for {pair}")
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
            equity=equity,
            margin_per_lot=margin_per_lot,
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

        pair_grids = self.grids.get(pair)
        for grid_manager in [pair_grids.long_grid, pair_grids.short_grid]:
            to_remove = []
            for pos in grid_manager.positions:
                if pos.position_id and str(pos.position_id) not in broker_ids:
                    to_remove.append(str(pos.position_id))

            for pos_id in to_remove:
                grid_manager.remove_position(pos_id)
                logger.info(f"TradingEngine: removed stale position {pos_id} from memory")

        spread = await client.get_spread(pair) or 0.0
        pair_info = client.get_pair_info(pair)
        lot_size_cents = _pair_lot_size_cents(pair_info) if pair_info else VOLUME_CENTS_PER_LOT

        for pos_data in broker_positions.values():
            if pos_data.get("symbol") != pair:
                continue
            self._restore_broker_position(
                client, pos_data, source="reconcile", spread=spread,
            )

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
        found = self.grids.find_position_by_client_order_id(client_order_id)
        if found is not None:
            pg, pos = found
            pg.grid_for(pos.direction).remove_position_by_client_order_id(client_order_id)

    def _effective_add_threshold(
        self,
        pair_grids: PairGrids,
        direction: Direction,
        degradation_mode: str,
    ) -> float:
        threshold = config.ADD_LEVEL_THRESHOLD
        if degradation_mode == DegradationMode.CONSERVATIVE:
            threshold += config.CONSERVATIVE_SCORE_PENALTY

        pair_long = pair_grids.pair_long_lots()
        pair_short = pair_grids.pair_short_lots()
        pair_total = pair_long + pair_short
        if pair_total > 0:
            imbalance = abs(pair_long - pair_short) / pair_total
            if imbalance >= config.IMBALANCE_SOFT_THRESHOLD:
                if direction == Direction.LONG and pair_long > pair_short:
                    threshold += config.IMBALANCE_SCORE_PENALTY
                elif direction == Direction.SHORT and pair_short > pair_long:
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
        equity: float,
        margin_per_lot: float,
    ) -> None:
        if config.TRADING_MODE != "AUTO":
            return
        if not _is_within_trade_window():
            return

        pair_grids = self.grids.get(pair)
        grid_manager = pair_grids.grid_for(direction)

        has_pair_level = pair_grids.is_active(direction)

        if not has_pair_level:
            if score < config.ENTRY_THRESHOLD:
                logger.debug(
                    f"TradingEngine: [{pair}] {direction.value} score={score:.1f} "
                    f"< entry {config.ENTRY_THRESHOLD}",
                )
                return
        else:
            threshold = self._effective_add_threshold(pair_grids, direction, degradation_mode)
            if score < threshold:
                return
            if not self._can_add_level(grid_manager, current_price, degradation_mode):
                return

        volume = await self._calculate_position_size(
            client, pair, current_atr, degradation_mode, equity=equity,
        )
        if volume <= 0:
            return

        logger.info(
            f"TradingEngine: [{pair}] {direction.value} setup score={score:.1f}, "
            f"vol={volume:.4f} lot",
        )

        if not self.portfolio.can_expand(
            pair,
            direction,
            pair_grids,
            self.grids,
            equity,
            margin_per_lot,
            additional_lots=volume,
            margin_by_pair=self._margin_per_lot_by_pair,
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
        *,
        equity: float,
    ) -> float:
        if equity <= 0:
            logger.critical(f"equity <= 0 for {pair}")
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
            lot_size_cents = _pair_lot_size_cents(pair_info)
        except Exception as e:
            logger.critical(f"get_pair_info error for {pair}: {e}")
            return 0.0

        if pip_value == 0:
            logger.warning("pip_value == 0, execution rejection")
            self.portfolio.increment_execution_rejection()
            return 0.0

        risk_amount = equity * config.RISK_PER_TRADE
        # pip_value from API is price pip size (e.g. 0.0001); monetary risk uses lot units.
        raw_volume = risk_amount / (sl_distance_price * PIP_LOT_UNITS)
        logger.debug(
            f"TradingEngine: position size {pair} equity={equity:.2f} risk={risk_amount:.2f} "
            f"sl_dist={sl_distance_price:.5f} raw_lot={raw_volume:.4f}",
        )

        if degradation_mode == DegradationMode.CONSERVATIVE:
            raw_volume *= config.CONSERVATIVE_VOLUME_MULTIPLIER

        resolved = resolve_order_volume(
            raw_volume, min_volume_cents, step_volume_cents, lot_size_cents=lot_size_cents,
        )
        volume = resolved.actual_lot
        if resolved.bumped_to_min:
            logger.debug(
                f"TradingEngine: [{pair}] volume bumped to broker min "
                f"{format_volume(resolved.actual_volume_cents, lot_size_cents)}",
            )
        if volume > config.MAX_LOT:
            logger.warning(
                f"TradingEngine: calculated lot {volume:.2f} > MAX_LOT {config.MAX_LOT}, capping",
            )
            volume = config.MAX_LOT
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
        digits = int(pair_info[2])
        entry = round(current_price, digits)
        stop_loss = round(stop_loss, digits)

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
                pair=pair,
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
            entry=entry,
            stop_loss=stop_loss,
            multiplier=1,
            min_volume=min_volume_cents,
            step_volume=step_volume_cents,
            pair=pair,
        )

        if result is None:
            try:
                rel_sl = relative_stop_loss_distance(entry, stop_loss, digits=digits)
            except ValueError:
                rel_sl = "—"
            logger.error(
                f"place_limit_order failed for {pair}: entry={entry} sl={stop_loss} "
                f"rel_sl={rel_sl} lot={volume:.4f} atr={current_atr:.4f} digits={digits}",
            )
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
            pair=pair,
            order_placed_at=now,
            grid_step_at_open=grid_step,
            level_index=level_index,
            signal_score_at_open=score,
        )
        grid_manager.add_position(position)

        logger.info(
            f"TradingEngine: placed grid order {client_order_id} for {pair}, "
            f"direction={direction.value}, level={level_index}, "
            f"vol={format_volume(resolved_volume.actual_volume_cents, resolved_volume.lot_size_cents)}",
        )
        return True

    async def _resolve_pending_before_close_global(self, client: Any) -> None:
        for pair in self._watched_pairs():
            await self._resolve_pending_before_close(client, pair)

    async def _resolve_pending_before_close(self, client: Any, pair: str) -> None:
        """Resolve position_id=None entries and cancel unresolvable pending orders."""
        spread = await client.get_spread(pair) or 0.0
        broker_positions = await client.get_positions(force=True)

        pair_grids = self.grids.get(pair)
        for grid_manager in [pair_grids.long_grid, pair_grids.short_grid]:
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

    async def _close_all_positions_global(self, client: Any, orchestrator: Any) -> None:
        await self._cancel_all_pending_orders_global(client, orchestrator)

        watched = self._watched_pairs()
        positions = await client.get_positions(force=True)
        for pos_data in positions:
            if pos_data.get("symbol") not in watched:
                continue
            pos_id = str(pos_data["id"])
            volume_cents = int(pos_data.get("volume", 0))
            ok = await self._close_position_with_verify(client, pos_id, volume_cents)
            if ok:
                logger.info(f"TradingEngine: closed position {pos_id}")
                found = self.grids.find_position_by_id(pos_id)
                if found is not None:
                    pg, pos = found
                    pg.grid_for(pos.direction).remove_position(pos_id)

        positions_after = await client.get_positions(force=True)
        remaining = [
            p for p in positions_after
            if p.get("symbol") in watched
        ]
        if remaining:
            logger.critical(
                "TradingEngine: positions still exist after close_all: "
                f"{[p['id'] for p in remaining]}",
            )
            self.portfolio.halted = True

    async def _close_worst_position_global(self, client: Any, orchestrator: Any) -> None:
        watched = self._watched_pairs()
        positions = [
            p for p in await client.get_positions(force=True)
            if p.get("symbol") in watched
        ]
        if not positions:
            return

        worst = min(positions, key=lambda p: p.get("profit", 0))
        pos_id = str(worst["id"])
        volume_cents = int(worst.get("volume", 0))

        ok = await self._close_position_with_verify(client, pos_id, volume_cents)
        if ok:
            logger.info(f"TradingEngine: closed worst position {pos_id}")
            found = self.grids.find_position_by_id(pos_id)
            if found is not None:
                pg, pos = found
                pg.grid_for(pos.direction).remove_position(pos_id)
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
            await self._cancel_all_pending_orders_global(client, orchestrator)

    async def _cancel_all_pending_orders_global(self, client: Any, orchestrator: Any) -> None:
        for pair in self._watched_pairs():
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
