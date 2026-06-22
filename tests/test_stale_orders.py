"""Тесты TTL pending limit/stop ордеров."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.strategy.orchestrator import StrategyOrchestrator
from app.strategy.trade_guard import TradeGuard
from app.trading.stale_orders import (
    _order_age_seconds,
    cancel_stale_pending_orders,
    pending_order_max_age_seconds,
)


class TestOrderAgeSeconds:

    def test_uses_last_update_when_present(self):
        now_ms = 1_700_000_000_000
        order = {"last_update_ms": now_ms - 600_000, "open_timestamp_ms": now_ms - 900_000}
        assert _order_age_seconds(order, now_ms) == pytest.approx(600.0)

    def test_falls_back_to_open_timestamp(self):
        now_ms = 1_700_000_000_000
        order = {"open_timestamp_ms": now_ms - 120_000}
        assert _order_age_seconds(order, now_ms) == pytest.approx(120.0)

    def test_missing_timestamp_returns_zero(self):
        assert _order_age_seconds({}, 1_700_000_000_000) == 0.0


class TestCancelStalePendingOrders:

    async def test_cancels_old_untracked_order(self, mock_market_cache, mock_pair_configs):
        client = MagicMock()
        client.get_pending_orders = AsyncMock(return_value=[
            {
                "pair": "EURUSD",
                "clientOrderId": "old-order",
                "orderId": 101,
                "open_timestamp_ms": int(time.time() * 1000) - 400_000,
            },
        ])
        client.cancel_order = AsyncMock(return_value=True)

        orch = StrategyOrchestrator(
            client=client,
            market_cache=mock_market_cache,
            pair_configs=mock_pair_configs,
        )
        orch.release_pair_blocks = AsyncMock()

        with patch(
            "app.trading.stale_orders.pending_order_max_age_seconds",
            return_value=300.0,
        ):
            n = await cancel_stale_pending_orders(client, orch, max_age_sec=300.0)

        assert n == 1
        client.cancel_order.assert_awaited_once_with(101)
        orch.release_pair_blocks.assert_awaited_once_with("EURUSD", "old-order")

    async def test_skips_fresh_order(self, mock_market_cache, mock_pair_configs):
        client = MagicMock()
        client.get_pending_orders = AsyncMock(return_value=[
            {
                "pair": "EURUSD",
                "clientOrderId": "fresh-order",
                "orderId": 102,
                "open_timestamp_ms": int(time.time() * 1000) - 60_000,
            },
        ])
        client.cancel_order = AsyncMock(return_value=True)

        orch = StrategyOrchestrator(
            client=client,
            market_cache=mock_market_cache,
            pair_configs=mock_pair_configs,
        )

        n = await cancel_stale_pending_orders(client, orch, max_age_sec=300.0)

        assert n == 0
        client.cancel_order.assert_not_awaited()

    async def test_skips_tracked_order_for_broker_cleanup(
        self, mock_market_cache, mock_pair_configs,
    ):
        """Tracked-ордера не трогает cancel_stale_pending_orders — их снимает orchestrator."""
        client = MagicMock()
        client.get_pending_orders = AsyncMock(return_value=[
            {
                "pair": "EURUSD",
                "clientOrderId": "tracked-order",
                "orderId": 103,
                "open_timestamp_ms": int(time.time() * 1000) - 400_000,
            },
        ])
        client.cancel_order = AsyncMock(return_value=True)

        orch = StrategyOrchestrator(
            client=client,
            market_cache=mock_market_cache,
            pair_configs=mock_pair_configs,
        )
        await orch.track_limit_order("EURUSD", "tracked-order")

        n = await cancel_stale_pending_orders(client, orch, max_age_sec=300.0)

        assert n == 0
        client.cancel_order.assert_not_awaited()


class TestOrchestratorTrackedCleanup:

    async def test_run_pending_order_cleanup_cancels_tracked_stale(
        self, mock_market_cache, mock_pair_configs,
    ):
        client = MagicMock()
        client.get_reconcile_state = AsyncMock(return_value=([], []))
        client.get_broker_order_id = MagicMock(return_value=201)
        client.cancel_order = AsyncMock(return_value=True)

        orch = StrategyOrchestrator(
            client=client,
            market_cache=mock_market_cache,
            pair_configs=mock_pair_configs,
        )
        await orch.track_limit_order("EURUSD", "tracked-stale")
        orch._limit_orders_by_pair["EURUSD"]["placed_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        )

        with patch(
            "app.trading.stale_orders.pending_order_max_age_seconds",
            return_value=300.0,
        ):
            n = await orch.run_pending_order_cleanup()

        assert n >= 1
        client.cancel_order.assert_awaited_once_with(201)
        assert "EURUSD" not in orch.get_tracked_limit_orders()


class TestTradeGuardStalePending:

    async def test_clears_pending_after_ttl(self):
        guard = TradeGuard()
        handler = AsyncMock()
        guard.set_stale_pending_handler(handler)

        with patch.object(guard, "_pending_timeout_sec", 300.0):
            await guard.mark_order_pending("order-1", "EURUSD")
            guard._pending_timestamps["EURUSD"] = (
                datetime.now(timezone.utc) - timedelta(seconds=301)
            )

            assert await guard.can_trade("EURUSD") is True

        pending = await guard.get_pending_orders()
        assert "EURUSD" not in pending
        handler.assert_awaited_once_with("EURUSD")

    async def test_stale_handler_releases_blocks_when_tracked_order_expired(
        self, mock_market_cache, mock_pair_configs,
    ):
        """После TTL снимаются и TradeGuard, и tracked-блокировки orchestrator."""
        orch = StrategyOrchestrator(
            client=MagicMock(),
            market_cache=mock_market_cache,
            pair_configs=mock_pair_configs,
        )
        await orch.track_limit_order("EURUSD", "expired-order")
        orch._limit_orders_by_pair["EURUSD"]["placed_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        )
        await orch.mark_order_pending("expired-order", "EURUSD")

        guard = orch._trade_guard
        guard.set_stale_pending_handler(orch._on_stale_pending)
        guard._pending_timeout_sec = 300.0
        guard._pending_timestamps["EURUSD"] = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        )

        with patch(
            "app.trading.stale_orders.pending_order_max_age_seconds",
            return_value=300.0,
        ):
            await guard._cleanup_stale_pending()

        assert await guard.get_pending_orders() == {}
        assert "EURUSD" not in orch.get_tracked_limit_orders()


class TestPendingOrderConfig:

    def test_default_ttl_from_config(self):
        from app.config.settings import config

        assert pending_order_max_age_seconds() == float(config.PENDING_ORDER_MAX_AGE_SECONDS)
        assert config.PENDING_ORDER_MAX_AGE_SECONDS > 0
        assert config.PENDING_ORDER_CLEANUP_INTERVAL_SECONDS > 0
