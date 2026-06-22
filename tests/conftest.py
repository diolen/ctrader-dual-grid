"""
tests/conftest.py
Общие фикстуры для всех тестов.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
async def demo_client():
    from app.connection.ctrader_client import CTraderClient

    client = CTraderClient()
    await client.connect()
    await client.authorize()
    await client.init_symbols()
    yield client
    await client.disconnect()


@pytest.fixture
async def demo_executor(demo_client):
    from app.trading.executor import TradeExecutor

    return TradeExecutor(demo_client)


def make_mock_ctrader_client(**overrides):
    """Мок cTrader-клиента с async-методами для orchestrator/recovery."""
    client = MagicMock()
    client.get_reconcile_state = AsyncMock(return_value=([], []))
    client.get_positions = AsyncMock(return_value=[])
    client.get_pending_orders = AsyncMock(return_value=[])
    client.fetch_reconcile_snapshot = AsyncMock(
        return_value={"positions": [], "orders": []},
    )
    client.get_position_close_profit = AsyncMock(return_value=None)
    client.get_broker_order_id = MagicMock(return_value=None)
    client.cancel_order = AsyncMock(return_value=False)
    client.cancel_order_by_client_id = AsyncMock(return_value=False)
    for name, value in overrides.items():
        setattr(client, name, value)
    return client


@pytest.fixture
def mock_client():
    return make_mock_ctrader_client()


@pytest.fixture
def mock_market_cache():
    cache = MagicMock()
    cache.get_metrics = MagicMock(return_value=None)
    cache.get_spread = MagicMock(return_value=None)
    cache.get_bid_ask = MagicMock(return_value=None)
    cache.is_pair_stale = MagicMock(return_value=True)
    cache.get_all_pairs = MagicMock(return_value=[])
    return cache


@pytest.fixture
def mock_pair_configs():
    from app.config.settings import config

    return {pair: config.get_pair_config(pair) for pair in ("EURUSD", "GBPUSD")}


@pytest.fixture
def orchestrator(mock_client, mock_market_cache, mock_pair_configs):
    from app.strategy.orchestrator import StrategyOrchestrator

    return StrategyOrchestrator(
        client=mock_client,
        market_cache=mock_market_cache,
        pair_configs=mock_pair_configs,
    )
