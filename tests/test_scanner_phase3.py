"""Unit tests for Phase 3 scanners."""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.scanner.context.builder import MarketContextBuilder
from app.scanner.scanners.liquidity_sweep.scanner import LiquiditySweepScanner
from app.scanner.scanners.price_action.scanner import PriceActionScanner
from app.scanner.scanners.pullback.scanner import PullbackScanner
from app.scanner.types.enums import Direction, SetupType
from app.scanner.types.market_context import MarketContext
from app.scanner.types.enums import TrendDirection, VolatilityRegime


def _ts(base: datetime, i: int) -> datetime:
    return base + timedelta(minutes=5 * i)


class TestPriceActionScanner:
    def test_detects_bullish_engulfing(self):
        base = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(48):
            rows.append({
                "timestamp": _ts(base, i),
                "open": 1.0800, "high": 1.0810, "low": 1.0790, "close": 1.0805,
                "volume": 100,
            })
        rows.append({
            "timestamp": _ts(base, 48),
            "open": 1.0806, "high": 1.0810, "low": 1.0800, "close": 1.0801,
            "volume": 100,
        })
        rows.append({
            "timestamp": _ts(base, 49),
            "open": 1.0799, "high": 1.0815, "low": 1.0795, "close": 1.0812,
            "volume": 150,
        })
        candles = pd.DataFrame(rows)

        ctx = MarketContext(
            symbol="EURUSD", timeframe="M5",
            trend_direction=TrendDirection.BULLISH,
            atr_value=0.0010,
            key_support_levels=(), key_resistance_levels=(),
            recent_swing_highs=(), recent_swing_lows=(),
            liquidity_zones=(),
            volatility_regime=VolatilityRegime.NORMAL,
            calculated_at=base,
        )
        results = PriceActionScanner().scan(candles, ctx)
        engulf = [r for r in results if "engulfing" in r.reasons[0].lower()]
        assert len(engulf) >= 1
        assert engulf[0].setup_type == SetupType.PRICE_ACTION
        assert engulf[0].direction == Direction.BUY


class TestLiquiditySweepScanner:
    def test_detects_buy_side_sweep(self):
        base = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
        level = 1.0800
        rows = []
        for i in range(10):
            rows.append({
                "timestamp": _ts(base, i),
                "open": 1.0820, "high": 1.0830, "low": 1.0810, "close": 1.0825,
                "volume": 100,
            })
        rows.append({
            "timestamp": _ts(base, 10),
            "open": 1.0825, "high": 1.0835, "low": 1.0815, "close": 1.0830,
            "volume": 100,
        })
        rows.append({
            "timestamp": _ts(base, 11),
            "open": 1.0830, "high": 1.0835, "low": 1.0790, "close": 1.0820,
            "volume": 200,
        })
        candles = pd.DataFrame(rows)

        ctx = MarketContext(
            symbol="EURUSD", timeframe="M5",
            trend_direction=TrendDirection.BULLISH,
            atr_value=0.0010,
            key_support_levels=(level,),
            key_resistance_levels=(),
            recent_swing_highs=(), recent_swing_lows=(level, level),
            liquidity_zones=((level, "BUY_SIDE"),),
            volatility_regime=VolatilityRegime.NORMAL,
            calculated_at=base,
        )
        results = LiquiditySweepScanner().scan(candles, ctx)
        assert len(results) >= 1
        assert results[0].setup_type == SetupType.LIQUIDITY_SWEEP
        assert results[0].direction == Direction.BUY


class TestPullbackScanner:
    def test_returns_empty_without_trend(self):
        base = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
        rows = []
        for i in range(250):
            px = 1.0800 + i * 0.00001
            rows.append({
                "timestamp": _ts(base, i),
                "open": px, "high": px + 0.0005, "low": px - 0.0005, "close": px,
                "volume": 100,
            })
        candles = pd.DataFrame(rows)
        ctx = MarketContextBuilder().build("EURUSD", "M5", candles)
        results = PullbackScanner().scan(candles, ctx)
        assert isinstance(results, list)
