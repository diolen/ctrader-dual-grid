"""
tests/test_orchestrator.py
Unit-тесты для Scalping StrategyOrchestrator.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock
from tests.conftest import make_mock_ctrader_client
from app.strategy.orchestrator import StrategyOrchestrator
from app.strategy.base import MarketData
from app.models.candle import Candle
from datetime import datetime, timezone


class TestStrategyOrchestrator:

    def test_initialization(self, orchestrator):
        assert orchestrator is not None
        assert orchestrator.get_active_strategy() is not None
        assert orchestrator.get_strategy_type() == "BREAKOUT_RETEST_V3"

    def test_timeframes(self, orchestrator):
        assert orchestrator.get_timeframe() == "M5"
        assert orchestrator.get_bias_timeframe() is None

    def test_trade_guard_methods(self, orchestrator):
        result = asyncio.run(orchestrator.can_trade("EURUSD"))
        assert result is True

    async def test_mark_order_pending(self, orchestrator):
        await orchestrator.mark_order_pending("test_order_123", "EURUSD")
        pending = await orchestrator.get_pending_orders()
        assert "EURUSD" in pending
        assert pending["EURUSD"] == "test_order_123"

    async def test_mark_order_complete(self, orchestrator):
        await orchestrator.mark_order_pending("test_order_456", "EURUSD")
        await orchestrator.mark_order_complete("test_order_456", "EURUSD")

        pending = await orchestrator.get_pending_orders()
        assert "EURUSD" not in pending

    async def test_reset_trade_guard(self, orchestrator):
        await orchestrator.mark_order_pending("test_order_789", "EURUSD")
        await orchestrator.reset_trade_guard()

        pending = await orchestrator.get_pending_orders()
        assert len(pending) == 0


class TestMarketData:

    def test_market_data_creation(self):
        candles = [
            Candle(
                timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
                open=1.0,
                high=1.1,
                low=0.9,
                close=1.05,
                volume=100,
            )
        ]

        market_data = MarketData(
            pair="EURUSD",
            candles=candles,
            extra_timeframes={"M15": candles},
        )

        assert market_data.pair == "EURUSD"
        assert len(market_data.candles) == 1
        assert "M15" in market_data.extra_timeframes


class TestOrchestratorWiring:
    def test_strategy_receives_pair_configs(self, orchestrator, mock_pair_configs):
        strategy = orchestrator.get_active_strategy()
        assert strategy.pair_configs == mock_pair_configs


class TestRecovery:

    async def test_reconcile_releases_when_limit_removed_at_broker(
        self, mock_market_cache, mock_pair_configs,
    ):
        mock_client = make_mock_ctrader_client()

        orch = StrategyOrchestrator(
            client=mock_client,
            market_cache=mock_market_cache,
            pair_configs=mock_pair_configs,
        )
        await orch.track_limit_order("GBPUSD", "client-uuid-1")

        removed = await orch.reconcile_missing_broker_pending(grace_sec=0)
        assert removed == 1
        assert "GBPUSD" not in orch.get_tracked_limit_orders()
