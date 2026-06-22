"""Unit-тесты Breakout Retest Scalping v3 (shallow retest + stop order)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.config.settings import config
from app.models.candle import Candle
from app.strategy.base import MarketData
from app.strategy.breakout_retest_v3 import (
    BreakoutRetestScalpingV3Strategy,
    BreakoutSetup,
    Level,
    build_signal,
    check_breakout_at,
    check_displacement,
    check_shallow_retest,
    detect_levels,
    displacement_size,
)
from app.strategy.fsm import StrategyState


def _bar(
    i: int,
    o: float,
    h: float,
    l: float,
    c: float,
    *,
    volume: int = 100,
    base: datetime | None = None,
) -> Candle:
    base = base or datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    return Candle(
        timestamp=base + timedelta(minutes=5 * i),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=volume,
    )


def _flat_range(n: int, low: float = 1.0800, high: float = 1.0820) -> list[Candle]:
    bars = []
    for i in range(n):
        mid = (low + high) / 2
        bars.append(_bar(i, mid, high, low, mid))
    return bars


@pytest.fixture
def pair_config():
    return config.get_pair_config("EURUSD")


@pytest.fixture
def strategy(pair_config):
    s = BreakoutRetestScalpingV3Strategy()
    s.pair_config = pair_config
    s.pip_value = 0.0001
    return s


class TestShallowRetest:
    def test_buy_shallow_retest_in_range(self):
        level = Level(
            price=1.0820,
            touches=3,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=15,
            max_high_since=1.0835,
            min_low_since=1.0815,
            setup_start_bar=18,
        )
        bars = _flat_range(22)
        idx = len(bars) - 2
        # peak 1.0830, disp 10p, low 1.08235 → retrace 65% + wick у level (4p tol)
        setup.max_high_since = 1.0830
        bars[idx] = _bar(idx, 1.0828, 1.0829, 1.08235, 1.0827)
        state, pct = check_shallow_retest(
            bars, idx, setup,
            min_retrace_percent=30.0,
            max_retrace_percent=70.0,
            retest_tolerance_pips=4.0,
            pip_value=0.0001,
        )
        assert state == "OK"
        assert pct == pytest.approx(65.0, rel=0.05)

    def test_buy_retrace_ok_without_proximity_waits(self):
        level = Level(
            price=1.0820,
            touches=3,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=15,
            max_high_since=1.0835,
            min_low_since=1.0815,
        )
        bars = _flat_range(22)
        idx = len(bars) - 2
        # retrace 33% OK, но low далеко от level (>4p)
        bars[idx] = _bar(idx, 1.0832, 1.0834, 1.0830, 1.0833)
        state, pct = check_shallow_retest(
            bars, idx, setup,
            min_retrace_percent=30.0,
            max_retrace_percent=70.0,
            retest_tolerance_pips=4.0,
            pip_value=0.0001,
        )
        assert state == "WAIT"
        assert pct == pytest.approx(33.33, rel=0.05)

    def test_buy_too_deep_cancels(self):
        level = Level(
            price=1.0820,
            touches=2,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=15,
            max_high_since=1.0835,
            min_low_since=1.0815,
        )
        bars = _flat_range(22)
        idx = len(bars) - 2
        # retrace 12p / 15p = 80% > 70%
        bars[idx] = _bar(idx, 1.0825, 1.0830, 1.0823, 1.0826)
        state, pct = check_shallow_retest(
            bars, idx, setup,
            min_retrace_percent=30.0,
            max_retrace_percent=70.0,
        )
        assert state == "CANCELLED"
        assert pct > 70.0

    def test_buy_wait_insufficient_retrace(self):
        level = Level(
            price=1.0820,
            touches=2,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=15,
            max_high_since=1.0835,
            min_low_since=1.0815,
        )
        bars = _flat_range(22)
        idx = len(bars) - 2
        bars[idx] = _bar(idx, 1.0833, 1.0836, 1.0832, 1.0834)
        state, pct = check_shallow_retest(
            bars, idx, setup,
            min_retrace_percent=30.0,
            max_retrace_percent=70.0,
        )
        assert state == "WAIT"
        assert pct is not None and pct < 30.0


class TestBuildSignalV3:
    def test_buy_stop_at_retest_high_no_tp(self):
        level = Level(
            price=1.0830,
            touches=2,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=18,
        )
        bars = _flat_range(22)
        for i in range(18, 21):
            bars[i] = _bar(i, 1.0825, 1.0832, 1.0818, 1.0830)
        retest_idx = 20
        bars[retest_idx] = _bar(retest_idx, 1.0830, 1.0838, 1.0828, 1.0832)
        bars[21] = _bar(21, 1.0832, 1.0840, 1.0830, 1.0836)

        sig = build_signal(
            setup,
            bars,
            "EURUSD",
            sl_buffer_pips=0.5,
            timeframe="M5",
            pip_value=0.0001,
            retest_bar_index=retest_idx,
        )
        assert sig.entry == pytest.approx(bars[retest_idx].high)
        assert sig.take_profit == 0.0
        assert sig.strategy_type == "BREAKOUT_RETEST_V3"
        assert sig.stop_loss < sig.entry

    def test_sell_stop_at_retest_low(self):
        level = Level(
            price=1.0800,
            touches=2,
            direction="SUPPORT",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="SELL",
            breakout_bar_index=18,
            max_high_since=1.0810,
            min_low_since=1.0785,
        )
        bars = _flat_range(22)
        retest_idx = 20
        bars[retest_idx] = _bar(retest_idx, 1.0790, 1.0795, 1.0788, 1.0792)
        sig = build_signal(
            setup,
            bars,
            "EURUSD",
            sl_buffer_pips=0.5,
            timeframe="M5",
            pip_value=0.0001,
            retest_bar_index=retest_idx,
        )
        assert sig.entry == pytest.approx(bars[retest_idx].low)
        assert sig.direction == "SELL"
        assert sig.take_profit == 0.0


class TestDisplacementSize:
    def test_buy_displacement(self):
        level = Level(
            price=1.0820,
            touches=2,
            direction="RESISTANCE",
            last_touch_bar=5,
            first_touch_bar=3,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=10,
            max_high_since=1.0835,
            min_low_since=1.0810,
        )
        assert displacement_size(setup) == pytest.approx(0.0015)


class TestFSMIntegrationV3:
    async def test_scan_to_displacement(self, strategy):
        pair = "EURUSD"
        level = 1.08200
        bars = []
        for i in range(28):
            if i in (6, 11, 16):
                bars.append(_bar(i, 1.0815, level, 1.0810, 1.0818))
            else:
                bars.append(_bar(i, 1.0812, 1.0816, 1.0808, 1.0814))
        bars.append(_bar(28, 1.0822, 1.0835, 1.0820, 1.0832))
        bars.append(_bar(29, 1.0832, 1.0836, 1.0830, 1.0834))

        with patch.object(strategy.trade_guard, "can_trade", new=AsyncMock(return_value=True)):
            sig = await strategy.update(MarketData(pair=pair, candles=bars, spread=0.0001))
        assert sig is None
        st = strategy._get_state(pair)
        assert st.fsm.current_state == StrategyState.DISPLACEMENT_WAIT

    async def test_shallow_retest_emits_stop_signal(self, strategy):
        pair = "EURUSD"
        level = Level(
            price=1.08200,
            touches=3,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=20,
            max_high_since=1.0830,
            min_low_since=1.0815,
            setup_start_bar=22,
        )
        st = strategy._get_state(pair)
        st.setup = setup
        st.fsm.transition_to(StrategyState.WAIT_RETEST)

        bars = _flat_range(30)
        idx = len(bars) - 2
        bars[idx] = _bar(idx, 1.0828, 1.0830, 1.0824, 1.0826)

        with patch.object(strategy.trade_guard, "can_trade", new=AsyncMock(return_value=True)):
            sig = await strategy.update(
                MarketData(pair=pair, candles=bars, spread=0.0001),
            )
        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.entry == pytest.approx(bars[idx].high)
        assert sig.take_profit == 0.0
        assert st.fsm.current_state == StrategyState.ENTRY_SUBMITTED


class TestPairStatusLine:
    def test_wait_retest_timeout_uses_per_pair_config(self, mock_pair_configs):
        strategy = BreakoutRetestScalpingV3Strategy()
        strategy.pair_configs = mock_pair_configs
        strategy.pair_config = mock_pair_configs["GBPUSD"]
        strategy.pip_value = 0.0001

        for pair, level_price, peak in (
            ("EURUSD", 1.15246, 1.15466),
            ("GBPUSD", 1.33376, 1.33616),
        ):
            setup = BreakoutSetup(
                level=Level(
                    price=level_price,
                    touches=3,
                    direction="RESISTANCE",
                    last_touch_bar=10,
                    first_touch_bar=5,
                ),
                direction="BUY",
                breakout_bar_index=5,
                max_high_since=peak,
                min_low_since=level_price - 0.0010,
                setup_start_bar=145,
            )
            st = strategy._get_state(pair)
            st.setup = setup
            st.fsm.transition_to(StrategyState.WAIT_RETEST)

        eur_line = strategy.pair_status_line("EURUSD", bars_count=150)
        gbp_line = strategy.pair_status_line("GBPUSD", bars_count=150)
        eur_timeout = mock_pair_configs["EURUSD"].retest_timeout_bars
        gbp_timeout = mock_pair_configs["GBPUSD"].retest_timeout_bars

        assert f"wait=3/{eur_timeout}" in eur_line
        assert f"wait=3/{gbp_timeout}" in gbp_line
        assert eur_timeout != gbp_timeout


class TestColdStartV3:
    def test_cold_start_does_not_crash(self, strategy):
        strategy.cold_start(_flat_range(120), "EURUSD")
        st = strategy._get_state("EURUSD")
        assert st.fsm.current_state in (
            StrategyState.SCAN_LEVELS,
            StrategyState.WAIT_RETEST,
            StrategyState.DISPLACEMENT_WAIT,
        )


class TestBreakoutLevelPriority:
    def test_prefers_level_with_more_touches(self):
        weak = Level(
            price=1.0820,
            touches=2,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        strong = Level(
            price=1.0821,
            touches=5,
            direction="RESISTANCE",
            last_touch_bar=8,
            first_touch_bar=4,
        )
        bars = _flat_range(20)
        idx = len(bars) - 2
        bars[idx] = _bar(idx, 1.0822, 1.0835, 1.0820, 1.0832, volume=200)

        setup, reject = check_breakout_at(
            bars,
            idx,
            [weak, strong],
            close_buffer=1.0,
            min_body=3.0,
            max_bars_since_touch=20,
            touch_tolerance=3.0,
            pip_value=0.0001,
        )
        assert reject is None
        assert setup is not None
        assert setup.level.touches == 5


class TestOrchestratorGuardFsm:
    @pytest.mark.skipif(
        not config.is_breakout_v3(),
        reason="только для STRATEGY_TYPE=BREAKOUT_RETEST_V3",
    )
    async def test_wait_retest_timeout_runs_with_pending_guard(
        self, orchestrator,
    ):
        pair = "EURUSD"
        strategy = orchestrator.get_active_strategy()
        level = Level(
            price=1.08200,
            touches=3,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=5,
            max_high_since=1.0830,
            min_low_since=1.0810,
            setup_start_bar=0,
        )
        st = strategy._get_state(pair)
        st.setup = setup
        st.fsm.transition_to(StrategyState.WAIT_RETEST)

        await orchestrator.mark_order_pending("pending-stop-1", pair)

        bars = _flat_range(30)
        idx = len(bars) - 2
        bars[idx] = _bar(idx, 1.0828, 1.0829, 1.0827, 1.0828)

        sig = await orchestrator.update(
            MarketData(pair=pair, candles=bars, spread=0.0001),
        )
        assert sig is None
        assert st.fsm.current_state == StrategyState.SCAN_LEVELS
        assert st.setup is None

    @pytest.mark.skipif(
        not config.is_breakout_v3(),
        reason="только для STRATEGY_TYPE=BREAKOUT_RETEST_V3",
    )
    async def test_emit_blocked_when_guard_busy(self, strategy):
        pair = "EURUSD"
        level = Level(
            price=1.08200,
            touches=3,
            direction="RESISTANCE",
            last_touch_bar=10,
            first_touch_bar=5,
        )
        setup = BreakoutSetup(
            level=level,
            direction="BUY",
            breakout_bar_index=20,
            max_high_since=1.0830,
            min_low_since=1.0815,
            setup_start_bar=22,
        )
        st = strategy._get_state(pair)
        st.setup = setup
        st.fsm.transition_to(StrategyState.WAIT_RETEST)

        bars = _flat_range(30)
        idx = len(bars) - 2
        bars[idx] = _bar(idx, 1.0828, 1.0830, 1.0824, 1.0828)

        await strategy.trade_guard.mark_order_pending("block-1", pair)
        sig = await strategy.update(
            MarketData(pair=pair, candles=bars, spread=0.0001),
        )
        assert sig is None
        assert st.fsm.current_state == StrategyState.WAIT_RETEST


class TestLevelDetectionUnchanged:
    def test_finds_resistance_with_touches(self):
        bars = _flat_range(30)
        for i in (10, 15, 20):
            bars[i] = _bar(i, 1.081, 1.0825, 1.0805, 1.0815)
        levels = detect_levels(
            bars,
            lookback_bars=50,
            min_touches=2,
            touch_tolerance=3.0,
            cluster_pips=5.0,
            min_level_age_bars=1,
            max_level_age_bars=200,
            pip_value=0.0001,
            current_bar=len(bars) - 2,
        )
        assert any(abs(l.price - 1.0825) < 0.0002 for l in levels)
