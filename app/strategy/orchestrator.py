# app/strategy/orchestrator.py
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple

from app.config.settings import config
from app.strategy.base import BaseStrategy, MarketData
from app.strategy.trade_guard import TradeGuard
from app.models.signal import Signal as UnifiedSignal


def create_breakout_strategy(trade_guard: TradeGuard) -> tuple[BaseStrategy, str]:
    """Фабрика live-стратегии Breakout Retest v3."""
    from app.strategy.breakout_retest_v3 import BreakoutRetestScalpingV3Strategy

    return BreakoutRetestScalpingV3Strategy(trade_guard=trade_guard), "BREAKOUT_RETEST_V3"


class StrategyOrchestrator:
    """
    Оркестратор Breakout Retest: TradeGuard, recovery, tracking pending-ордеров.
    """

    def __init__(
        self,
        client,
        market_cache,
        pair_configs: Dict[str, object],
    ):
        self._client = client
        self._market_cache = market_cache
        self._pair_configs = pair_configs
        self._trade_guard = TradeGuard()
        self._limit_orders_by_pair: Dict[str, dict] = {}

        self._strategy, self.STRATEGY_TYPE = create_breakout_strategy(self._trade_guard)
        if hasattr(self._strategy, "pair_configs"):
            self._strategy.pair_configs = self._pair_configs
        self._strategy.set_fsm_timeout_handler(self._on_fsm_timeout)
        ver = "v3" if config.is_breakout_v3() else "v2"
        logging.info(f"✅ Strategy: {type(self._strategy).__name__} ({ver}) инициализирована")
        self._wire_execution_handlers()

    def get_active_strategy(self) -> BaseStrategy:
        return self._strategy

    def get_strategy_type(self) -> str:
        return self.STRATEGY_TYPE

    def get_timeframe(self, pair: str = "") -> str:
        if pair and pair in self._pair_configs:
            return self._pair_configs[pair].entry_timeframe
        if self._pair_configs:
            return next(iter(self._pair_configs.values())).entry_timeframe
        return "M5"

    def get_bias_timeframe(self, pair: str = "") -> Optional[str]:
        return None

    def cold_start_pair(self, pair: str, candles: list) -> None:
        """Холодный старт FSM после прогрева M5."""
        self._strategy.pair_config = self._pair_configs.get(pair)
        if hasattr(self._client, "get_pair_info"):
            info = self._client.get_pair_info(pair)
            if info and len(info) > 3:
                self._strategy.pip_value = float(info[3])
        self._strategy.cold_start(candles, pair)

    async def update(self, market_data: MarketData) -> Optional[UnifiedSignal]:
        pair = market_data.pair
        self._strategy.pair_config = self._pair_configs.get(pair)
        if hasattr(self._client, "get_pair_info"):
            info = self._client.get_pair_info(pair)
            if info and len(info) > 3:
                self._strategy.pip_value = float(info[3])

        try:
            return await self._strategy.update(market_data)
        except Exception as e:
            logging.error(f"❌ Orchestrator: ошибка в strategy.update(): {e}", exc_info=True)
            return None

    async def can_trade(self, pair: str = "") -> bool:
        return await self._trade_guard.can_trade(pair)

    def _wire_execution_handlers(self) -> None:
        if hasattr(self._client, "set_execution_callback"):
            self._client.set_execution_callback(self.on_execution_event)
        self._trade_guard.set_stale_pending_handler(self._on_stale_pending)

    async def _on_stale_pending(self, pair: str) -> None:
        info = self._limit_orders_by_pair.get(pair)
        if info:
            from app.trading.stale_orders import pending_order_max_age_seconds

            placed_at = info.get("placed_at")
            if placed_at:
                age = (datetime.now(timezone.utc) - placed_at).total_seconds()
                max_age = pending_order_max_age_seconds()
                if age < max_age:
                    logging.debug(
                        f"⏳ [{pair}] TradeGuard pending снят, limit-ордер ещё активен "
                        f"({age:.0f}s / {max_age:.0f}s)"
                    )
                    return
        await self.release_pair_blocks(pair)

    def get_tracked_limit_orders(self) -> Dict[str, dict]:
        return self._limit_orders_by_pair.copy()

    async def release_pair_blocks(
        self, pair: str, client_order_id: Optional[str] = None,
    ) -> None:
        self._limit_orders_by_pair.pop(pair, None)
        await self.clear_pending(pair)
        if hasattr(self._strategy, "release_signal_block"):
            try:
                await self._strategy.release_signal_block(pair)
            except Exception as e:
                logging.warning(
                    f"⚠️ [{pair}] release_signal_block failed: {e}",
                    exc_info=True,
                )
        if client_order_id:
            logging.debug(f"🔓 [{pair}] Блокировки сняты (order={client_order_id})")

    async def track_limit_order(
        self, pair: str, client_order_id: str, strategy_type: str = "",
    ) -> None:
        self._limit_orders_by_pair[pair] = {
            "client_order_id": client_order_id,
            "strategy_type": strategy_type or self.STRATEGY_TYPE,
            "placed_at": datetime.now(timezone.utc),
        }
        logging.info(f"📌 [{pair}] Pending-ордер отслеживается: {client_order_id}")

    async def release_limit_order(
        self, pair: str, client_order_id: Optional[str] = None,
    ) -> None:
        await self.release_pair_blocks(pair, client_order_id)

    async def cancel_tracked_limit_order(self, pair: str) -> bool:
        info = self._limit_orders_by_pair.get(pair)
        if not info:
            return False

        client_order_id = info.get("client_order_id")
        broker_id = None
        if client_order_id and hasattr(self._client, "get_broker_order_id"):
            broker_id = self._client.get_broker_order_id(client_order_id)

        ok = False
        if broker_id:
            ok = await self._client.cancel_order(broker_id)
        elif client_order_id and hasattr(self._client, "cancel_order_by_client_id"):
            ok = await self._client.cancel_order_by_client_id(client_order_id)

        if ok:
            await self.release_limit_order(pair, client_order_id)
        return ok

    async def on_execution_event(
        self,
        exec_type,
        client_order_id: Optional[str],
        pair: str,
        broker_order_id=None,
        position_id=None,
        is_position_close: bool = False,
        deal_profit: Optional[float] = None,
    ) -> None:
        import ctrader_open_api.messages.OpenApiModelMessages_pb2 as model_proto

        _ = broker_order_id

        if not pair:
            return

        if is_position_close:
            logging.info(f"📊 [{pair}] позиция закрыта у брокера")
            await self.release_pair_blocks(pair)
            return

        if exec_type == model_proto.ORDER_FILLED:
            self._limit_orders_by_pair.pop(pair, None)
            if hasattr(self._strategy, "on_order_filled"):
                self._strategy.on_order_filled(pair)
            if client_order_id:
                await self.mark_order_complete(client_order_id, pair)
        elif exec_type in (
            model_proto.ORDER_CANCELLED,
            model_proto.ORDER_EXPIRED,
            model_proto.ORDER_REJECTED,
        ):
            await self.release_limit_order(pair, client_order_id)

    async def needs_broker_reconcile(self) -> bool:
        if self._limit_orders_by_pair:
            return True
        pending = await self._trade_guard.get_pending_orders()
        return bool(pending)

    @staticmethod
    def _reconcile_grace_seconds() -> float:
        return float(config.PENDING_ORDER_MAX_AGE_SECONDS)

    async def _fetch_broker_state(
        self, *, force: bool = False,
    ) -> Tuple[List[dict], List[dict]]:
        if hasattr(self._client, "get_reconcile_state"):
            return await self._client.get_reconcile_state(force=force)
        positions = await self._client.get_positions()
        pending = await self._client.get_pending_orders()
        return positions, pending

    async def reconcile_missing_broker_pending(
        self,
        grace_sec: Optional[float] = None,
        *,
        pending_orders: Optional[List[dict]] = None,
        positions: Optional[List[dict]] = None,
    ) -> int:
        if grace_sec is None:
            grace_sec = self._reconcile_grace_seconds()

        try:
            if pending_orders is None or positions is None:
                broker_positions, broker_pending = await self._fetch_broker_state(
                    force=True,
                )
                pending_orders = broker_pending if pending_orders is None else pending_orders
                positions = broker_positions if positions is None else positions
        except Exception as e:
            logging.error(f"❌ Reconcile pending: {e}", exc_info=True)
            return 0

        pending_client_ids = {
            o.get("clientOrderId")
            for o in pending_orders
            if o.get("clientOrderId")
        }
        position_pairs = {
            p.get("symbol") for p in positions if isinstance(p, dict) and p.get("symbol")
        }
        now = datetime.now(timezone.utc)
        removed = 0

        for pair, info in list(self._limit_orders_by_pair.items()):
            cid = info.get("client_order_id") or ""
            placed_at = info.get("placed_at")

            if placed_at and (now - placed_at).total_seconds() < grace_sec:
                continue

            if cid and cid in pending_client_ids:
                continue

            if pair in position_pairs:
                self._limit_orders_by_pair.pop(pair, None)
                if hasattr(self._strategy, "on_order_filled"):
                    self._strategy.on_order_filled(pair)
                if cid:
                    await self.mark_order_complete(cid, pair)
                continue

            removed += 1
            await self.release_limit_order(pair, cid or None)

        return removed

    async def _on_fsm_timeout(self, pair: str) -> None:
        logging.warning(f"⏰ [{pair}] FSM timeout — отмена limit-ордера и сброс блокировок")
        await self.cancel_tracked_limit_order(pair)
        await self.release_pair_blocks(pair)

    async def run_pending_order_cleanup(self) -> int:
        from app.trading.stale_orders import (
            cancel_stale_pending_orders,
            pending_order_max_age_seconds,
        )

        if not await self.needs_broker_reconcile():
            return 0

        try:
            positions, pending_orders = await self._fetch_broker_state(force=True)
        except Exception as e:
            logging.error(f"❌ Cleanup: reconcile snapshot: {e}", exc_info=True)
            return 0

        removed = await self.reconcile_missing_broker_pending(
            pending_orders=pending_orders,
            positions=positions,
        )

        cancelled = 0
        now = datetime.now(timezone.utc)

        for pair, info in list(self._limit_orders_by_pair.items()):
            placed_at = info.get("placed_at")
            if not placed_at:
                continue
            age = (now - placed_at).total_seconds()
            max_age = pending_order_max_age_seconds()
            if age < max_age:
                continue
            logging.warning(
                f"⏰ [{pair}] Tracked limit-ордер устарел ({age:.0f}s > {max_age:.0f}s)"
            )
            if await self.cancel_tracked_limit_order(pair):
                cancelled += 1
            await self.release_pair_blocks(pair, info.get("client_order_id"))

        cancelled += await cancel_stale_pending_orders(
            self._client,
            self,
            pending_orders=pending_orders,
        )

        if self._limit_orders_by_pair:
            removed += await self.reconcile_missing_broker_pending(
                pending_orders=pending_orders,
                positions=positions,
            )

        if removed:
            logging.info(f"🗑️ Снято отслеживание лимиток (ордер снят у брокера): {removed}")
        return cancelled + removed

    async def clear_pending(self, pair: str) -> None:
        await self._trade_guard.clear_pending(pair)

    async def mark_order_pending(self, order_id: str, pair: str) -> None:
        await self._trade_guard.mark_order_pending(order_id, pair)

    async def mark_order_complete(self, order_id: str, pair: str = "") -> None:
        await self._trade_guard.mark_order_complete(order_id, pair)

    async def get_pending_orders(self) -> Dict[str, str]:
        return await self._trade_guard.get_pending_orders()

    async def reset_trade_guard(self) -> None:
        await self._trade_guard.reset()

    async def run_recovery(self):
        from app.trading.stale_orders import cancel_stale_pending_orders

        logging.info("🔄 Orchestrator: запуск recovery logic...")

        broker_pending: List[dict] = []

        try:
            _, broker_pending = await self._fetch_broker_state(force=True)
            stale = await cancel_stale_pending_orders(
                self._client,
                self,
                pending_orders=broker_pending,
            )
            if stale:
                logging.info(f"🧹 Recovery: отменено зависших pending-ордеров: {stale}")
                _, broker_pending = await self._fetch_broker_state(force=True)

            n = await self.reconcile_missing_broker_pending(
                grace_sec=0,
                pending_orders=broker_pending,
            )
            if n:
                logging.info(
                    f"🗑️ Recovery: снято отслеживание лимиток без ордера у брокера: {n}"
                )
        except Exception as e:
            logging.error(f"❌ Orchestrator: recovery: {e}", exc_info=True)

        logging.info("✅ Orchestrator: recovery logic завершен")
