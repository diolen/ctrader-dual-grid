"""
Integration smoke tests for TradingEngine on cTrader Demo.

Запуск:
    pytest tests/test_trading_engine_integration.py -v -m integration -s
"""

import datetime
import time

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.trading.trading_engine import TradingEngine


pytestmark = pytest.mark.integration


def _is_market_open() -> bool:
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() >= 5:
        return False
    if now.weekday() == 4 and now.hour >= 22:
        return False
    return True


market_open = pytest.mark.skipif(
    not _is_market_open(),
    reason="Demo API недоступен в выходные — запусти в Пн–Пт 00:00–22:00 UTC",
)


def _make_candles_from_trendbars(trendbars):
    from app.models.candle import Candle

    return list(trendbars) if trendbars and hasattr(trendbars[0], "timestamp") else []


@market_open
class TestTradingEngineDemoSmoke:
    """Dry-run TradingEngine cycle against live Demo API (no orders)."""

    async def test_on_bar_update_dry_run(self, demo_client, monkeypatch):
        """
        Полный цикл on_bar_update: margin cache, ATR, scan — без выставления ордеров.
        TRADING_MODE=MANUAL гарантирует отсутствие place_limit_order.
        """
        from app.config.settings import config as app_config
        from app.trading import trading_engine as te
        import ctrader_open_api.messages.OpenApiModelMessages_pb2 as model_proto

        class _ConfigProxy:
            def __getattr__(self, name):
                if name == "TRADING_MODE":
                    return "MANUAL"
                if name == "WATCHED_INSTRUMENTS":
                    return [("EURUSD", "M5")]
                return getattr(app_config, name)

        monkeypatch.setattr(te, "config", _ConfigProxy())

        pair = "EURUSD"
        info = demo_client.get_pair_info(pair)
        assert info is not None
        symbol_id = info[0]

        now = int(time.time())
        bar_ms = 5 * 60 * 1000
        trendbars = await demo_client.get_trendbars_chunked(
            symbol_id,
            model_proto.M5,
            (now - 200 * 5 * 60) * 1000,
            (now - 60) * 1000,
            bar_ms=bar_ms,
            pair=pair,
            timeframe="M5",
        )
        assert len(trendbars) >= app_config.ATR_PERIOD + 1

        candles = _make_candles_from_trendbars(trendbars)
        state = MagicMock(
            pair=pair,
            symbol_id=symbol_id,
            candles=candles,
            entry_tf="M5",
            entry_minutes=5,
        )

        orchestrator = AsyncMock()
        engine = TradingEngine()

        await engine.on_bar_update(state, orchestrator, demo_client, None)

        assert engine.portfolio.margin_per_lot > 0
        orchestrator.track_limit_order.assert_not_called()

    async def test_get_expected_margin_for_one_lot(self, demo_client):
        """Margin per 1 lot кэшируется корректно (используется в can_expand)."""
        from app.trading.volume import lot_to_volume_cents

        pair = "EURUSD"
        info = demo_client.get_pair_info(pair)
        symbol_id = info[0]

        margin = await demo_client.get_expected_margin(
            symbol_id, volume_cents=lot_to_volume_cents(1.0),
        )

        assert margin is not None
        assert margin > 0
