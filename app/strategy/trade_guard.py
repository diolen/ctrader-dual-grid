# app/strategy/trade_guard.py
import asyncio
import logging
from typing import Set, Optional, Dict, Callable, Awaitable, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class TradeGuard:
    """
    Async-safe защита от дублирования лимитных ордеров.
    Отслеживает только pending (ожидающие fill), не открытые позиции.
    """

    def __init__(self):
        from app.config.settings import config

        self._lock = asyncio.Lock()
        self._pending_by_pair: Dict[str, str] = {}
        self._pending_timestamps: Dict[str, datetime] = {}
        self._last_order_time: Optional[datetime] = None
        self._min_order_interval = config.BREAKOUT_MIN_ORDER_INTERVAL_SECONDS
        self._pending_timeout_sec = float(config.PENDING_ORDER_MAX_AGE_SECONDS)
        self._on_stale_pending: Optional[Callable[[str], Awaitable[Any]]] = None

    def set_stale_pending_handler(
        self, handler: Optional[Callable[[str], Awaitable[Any]]],
    ) -> None:
        self._on_stale_pending = handler

    async def can_trade(self, pair: str = "") -> bool:
        async with self._lock:
            await self._cleanup_stale_pending()

            if self._last_order_time:
                elapsed = (datetime.now(timezone.utc) - self._last_order_time).total_seconds()
                if elapsed < self._min_order_interval:
                    logger.warning(
                        f"⚠️ TradeGuard: ограничение частоты "
                        f"({elapsed:.2f}s < {self._min_order_interval}s)"
                    )
                    return False

            if pair and pair in self._pending_by_pair:
                logger.warning(
                    f"⚠️ TradeGuard: ожидающий лимит для {pair} "
                    f"(order_id={self._pending_by_pair[pair]})"
                )
                return False

            return True

    async def clear_pending(self, pair: str) -> None:
        async with self._lock:
            if pair in self._pending_by_pair:
                self._pending_by_pair.pop(pair, None)
                self._pending_timestamps.pop(pair, None)
                logger.debug(f"🔓 TradeGuard: {pair} → pending снят")

    async def mark_order_pending(self, order_id: str, pair: str) -> None:
        async with self._lock:
            self._pending_by_pair[pair] = order_id
            self._pending_timestamps[pair] = datetime.now(timezone.utc)
            self._last_order_time = datetime.now(timezone.utc)
            logger.debug(f"🔒 TradeGuard: {pair} → ожидает лимит (order_id={order_id})")

    async def mark_order_complete(self, order_id: str, pair: str = "") -> None:
        """Лимит принят/исполнен — снимает pending, позиции не отслеживаем."""
        async with self._lock:
            self._pending_by_pair.pop(pair, None)
            self._pending_timestamps.pop(pair, None)
            logger.debug(f"✅ TradeGuard: {pair} → pending снят (order_id={order_id})")

    async def get_pending_orders(self) -> Dict[str, str]:
        async with self._lock:
            return self._pending_by_pair.copy()

    async def reset(self, pair: Optional[str] = None) -> None:
        async with self._lock:
            if pair:
                self._pending_by_pair.pop(pair, None)
                self._pending_timestamps.pop(pair, None)
                logger.debug(f"🔄 TradeGuard: состояние сброшено для {pair}")
            else:
                self._pending_by_pair.clear()
                self._pending_timestamps.clear()
                self._last_order_time = None
                logger.info("🔄 TradeGuard: состояние полностью сброшено")

    async def _cleanup_stale_pending(self) -> None:
        now = datetime.now(timezone.utc)
        stale_pairs = []

        for pair, timestamp in self._pending_timestamps.items():
            elapsed = (now - timestamp).total_seconds()
            if elapsed > self._pending_timeout_sec:
                stale_pairs.append(pair)
                logger.warning(
                    f"⏰ TradeGuard: устаревший лимит для {pair} "
                    f"({elapsed:.0f}s), удаление"
                )

        for pair in stale_pairs:
            self._pending_by_pair.pop(pair, None)
            self._pending_timestamps.pop(pair, None)
            if self._on_stale_pending:
                try:
                    await self._on_stale_pending(pair)
                except Exception as e:
                    logger.warning(
                        f"⚠️ TradeGuard: stale pending handler failed for {pair}: {e}",
                        exc_info=True,
                    )
