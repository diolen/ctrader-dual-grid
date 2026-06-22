"""Unit tests for Phase 1–2 multi-setup scanner core."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.config.settings import config
from app.scanner.context.builder import MarketContextBuilder
from app.scanner.scanners.breakout.fsm import BreakoutFSMState, apply_transition
from app.scanner.scanners.breakout.scanner import BreakoutScanner
from app.scanner.types.enums import BreakoutState, TrendDirection
from app.scanner.types.setup_candidate import SetupCandidate
from app.scanner.utils.candles import closed_bars_slice, signal_bar_index


def _make_candles(n: int = 250, base: float = 1.0800) -> pd.DataFrame:
    start = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        o = base + (i % 7) * 0.0001
        h = o + 0.0010
        l = o - 0.0010
        c = o + 0.0002
        rows.append(
            {
                "timestamp": start + timedelta(minutes=5 * i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 100 + i,
            }
        )
    return pd.DataFrame(rows)


class TestMarketContext:
    def test_build_returns_frozen_context(self):
        candles = _make_candles()
        builder = MarketContextBuilder()
        ctx = builder.build("EURUSD", "M5", candles)

        assert ctx.symbol == "EURUSD"
        assert ctx.timeframe == "M5"
        assert ctx.atr_value > 0
        assert isinstance(ctx.trend_direction, TrendDirection)
        assert isinstance(ctx.key_support_levels, tuple)
        assert isinstance(ctx.key_resistance_levels, tuple)

        with pytest.raises(Exception):
            ctx.atr_value = 1.0  # frozen


class TestCandleUtils:
    def test_closed_bars_slice_excludes_forming_bar(self):
        candles = _make_candles(20)
        sliced = closed_bars_slice(candles, 5)
        assert len(sliced) == 5
        assert sliced.iloc[-1]["timestamp"] == candles.iloc[-2]["timestamp"]

    def test_signal_bar_index_closed_only(self):
        candles = _make_candles(10)
        assert signal_bar_index(candles) == 9
        assert signal_bar_index(candles, forming_bar_may_exist=True) == 8


class TestBreakoutFSM:
    def test_apply_transition_is_immutable(self):
        initial = BreakoutFSMState(state=BreakoutState.IDLE)
        nxt = apply_transition(initial, BreakoutState.BREAKOUT_DETECTED)
        assert initial.state == BreakoutState.IDLE
        assert nxt.state == BreakoutState.BREAKOUT_DETECTED


class TestSetupCandidate:
    def test_rr_ratio_computed(self):
        c = SetupCandidate(
            entry_price=1.0850,
            stop_loss=1.0820,
            take_profit=1.0910,
        )
        assert c.rr_ratio == pytest.approx(2.0)


class TestBreakoutScanner:
    def test_scan_idempotent_on_same_bar(self):
        candles = _make_candles()
        builder = MarketContextBuilder()
        ctx = builder.build("EURUSD", "M5", candles)

        scanner = BreakoutScanner(
            pip_value=0.0001,
            pair_config=config.get_pair_config("EURUSD"),
        )
        first = scanner.scan(candles, ctx)
        second = scanner.scan(candles, ctx)
        assert second == []
        assert isinstance(first, list)
