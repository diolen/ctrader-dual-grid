"""Unit-тесты SCREENER_ONLY: recommender, main helpers."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.config.settings import DEFAULT_PAIRS, METAL_CRYPTO_PAIRS, config
from app.core.recommender import AnalysisResult, MarketPhase, MarketRecommender
from app.models.candle import Candle
from app.strategy.breakout_retest_v3 import BreakoutRetestScalpingV3Strategy
from app.strategy.fsm import StrategyState
from app.strategy.trade_guard import TradeGuard
from main import (
    _PairRuntime,
    _apply_strategy_pair_config,
    _spread_price,
)


def _bar(i: int, close: float = 1.1000) -> Candle:
    base = datetime(2024, 6, 9, 10, 0, tzinfo=timezone.utc)
    return Candle(
        timestamp=base + timedelta(minutes=5 * i),
        open=close,
        high=close + 0.001,
        low=close - 0.001,
        close=close,
        volume=100,
    )


def _make_client(pair: str = "EURUSD", pip_value: float = 0.0001):
    client = MagicMock()
    client.get_pair_info.return_value = (1, pair, 5, pip_value, 100000, 0.01)
    return client


def _make_strategy(pairs: tuple[str, ...] = ("EURUSD",)) -> BreakoutRetestScalpingV3Strategy:
    strategy = BreakoutRetestScalpingV3Strategy(trade_guard=TradeGuard())
    strategy.pair_configs = {p: config.get_pair_config(p) for p in pairs}
    return strategy


class TestDefaultPairs:

    @pytest.mark.parametrize("pair", DEFAULT_PAIRS)
    def test_pair_config_loads(self, pair: str):
        cfg = config.get_pair_config(pair)
        assert cfg.pair == pair
        assert cfg.entry_timeframe == "M5"
        assert cfg.min_displacement_pips > 0

    @pytest.mark.parametrize("pair", METAL_CRYPTO_PAIRS)
    def test_metal_crypto_wider_thresholds(self, pair: str):
        cfg = config.get_pair_config(pair)
        eurusd = config.get_pair_config("EURUSD")
        assert cfg.max_spread_pips > eurusd.max_spread_pips
        assert cfg.min_displacement_pips > eurusd.min_displacement_pips


class TestSpreadPrice:

    def test_none_market_cache_returns_none(self):
        client = _make_client()
        assert _spread_price(None, client, "EURUSD") is None


class TestApplyStrategyPairConfig:

    def test_sets_pair_config_and_pip_value(self):
        strategy = _make_strategy()
        client = _make_client(pip_value=0.00012)

        _apply_strategy_pair_config(strategy, client, "EURUSD")

        assert strategy.pair_config is not None
        assert strategy.pair_config.pair == "EURUSD"
        assert strategy.pip_value == pytest.approx(0.00012)






class TestRecommender:

    def test_scan_levels_recommendation(self):
        result = AnalysisResult(
            pair="EURUSD",
            phase=MarketPhase.SCAN_LEVELS,
            current_price=1.1,
            key_level=None,
            direction=None,
            displacement_pips=None,
            retrace_percent=None,
            wait_bars=None,
            timeout_bars=None,
        )
        text = MarketRecommender.phase_to_recommendation(result)
        assert "Поиск уровней" in text

    def test_format_status_line_contains_pair(self):
        result = AnalysisResult(
            pair="GBPUSD",
            phase=MarketPhase.DISPLACEMENT_WAIT,
            current_price=1.25,
            key_level=1.24900,
            direction="BUY",
            displacement_pips=8.0,
            retrace_percent=None,
            wait_bars=None,
            timeout_bars=None,
        )
        line = MarketRecommender.format_status_line(result)
        assert "GBPUSD" in line
        assert "DISPLACEMENT_WAIT" in line


class TestGetAnalysisResult:

    def test_fsm_to_market_phase_mapping(self):
        strategy = _make_strategy()
        _apply_strategy_pair_config(strategy, _make_client(), "EURUSD")

        candles = [_bar(i) for i in range(10)]
        st = strategy._get_state("EURUSD")
        st.fsm._current_state = StrategyState.CANCELLED

        result = strategy.get_analysis_result("EURUSD", candles)

        assert result is not None
        assert result.phase == MarketPhase.SETUP_MISSED
        assert result.current_price == pytest.approx(1.1)




