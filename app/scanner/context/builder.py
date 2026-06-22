"""Build shared MarketContext once per symbol/timeframe."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.scanner.context.indicators import (
    cluster_liquidity_zones,
    cluster_prices,
    compute_atr,
    compute_ema,
    detect_swing_points,
)
from app.scanner.types.enums import TrendDirection, VolatilityRegime
from app.scanner.types.market_context import MarketContext
from app.scanner.utils.candles import validate_candles_df


class MarketContextBuilder:
    """
    Computes MarketContext exactly once per symbol/timeframe update.

    All derived market data lives here — scanners must not recompute.
    """

    def __init__(
        self,
        *,
        atr_period: int = 14,
        swing_lookback: int = 80,
        level_cluster_atr_mult: float = 0.5,
        trend_ema_fast: int = 50,
        trend_ema_slow: int = 200,
        ranging_threshold: float = 0.0005,
    ) -> None:
        self._atr_period = atr_period
        self._swing_lookback = swing_lookback
        self._level_cluster_atr_mult = level_cluster_atr_mult
        self._trend_ema_fast = trend_ema_fast
        self._trend_ema_slow = trend_ema_slow
        self._ranging_threshold = ranging_threshold

    def build(
        self,
        symbol: str,
        timeframe: str,
        candles: pd.DataFrame,
    ) -> MarketContext:
        validate_candles_df(candles)

        if candles.empty:
            now = datetime.now(timezone.utc)
            return MarketContext(
                symbol=symbol,
                timeframe=timeframe,
                trend_direction=TrendDirection.RANGING,
                atr_value=0.0,
                key_support_levels=(),
                key_resistance_levels=(),
                recent_swing_highs=(),
                recent_swing_lows=(),
                liquidity_zones=(),
                volatility_regime=VolatilityRegime.NORMAL,
                calculated_at=now,
            )

        closed = candles.iloc[:-1] if len(candles) > 1 else candles
        atr_series = compute_atr(closed, self._atr_period)
        atr_value = float(atr_series.iloc[-1]) if not atr_series.empty else 0.0

        swing_highs, swing_lows = detect_swing_points(
            closed, lookback=self._swing_lookback,
        )
        high_prices = [p for _, p in swing_highs[-10:]]
        low_prices = [p for _, p in swing_lows[-10:]]

        cluster_tol = max(atr_value * self._level_cluster_atr_mult, 1e-10)
        resistance_levels = tuple(
            cluster_prices(high_prices, cluster_tol),
        )
        support_levels = tuple(
            cluster_prices(low_prices, cluster_tol),
        )

        liquidity_zones = cluster_liquidity_zones(
            high_prices, low_prices, atr_value,
        )

        close = closed["close"]
        ema_fast = compute_ema(close, self._trend_ema_fast)
        ema_slow = compute_ema(close, self._trend_ema_slow)
        if len(ema_fast) >= self._trend_ema_slow and len(ema_slow) >= self._trend_ema_slow:
            diff = float(ema_fast.iloc[-1] - ema_slow.iloc[-1])
            rel = abs(diff) / float(close.iloc[-1]) if close.iloc[-1] else 0.0
            if rel < self._ranging_threshold:
                trend = TrendDirection.RANGING
            elif diff > 0:
                trend = TrendDirection.BULLISH
            else:
                trend = TrendDirection.BEARISH
        else:
            trend = TrendDirection.RANGING

        if len(atr_series) >= 20:
            atr_median = float(atr_series.iloc[-20:].median())
            if atr_value < 0.8 * atr_median:
                vol_regime = VolatilityRegime.LOW
            elif atr_value > 1.2 * atr_median:
                vol_regime = VolatilityRegime.HIGH
            else:
                vol_regime = VolatilityRegime.NORMAL
        else:
            vol_regime = VolatilityRegime.NORMAL

        return MarketContext(
            symbol=symbol,
            timeframe=timeframe,
            trend_direction=trend,
            atr_value=atr_value,
            key_support_levels=support_levels,
            key_resistance_levels=resistance_levels,
            recent_swing_highs=tuple(high_prices),
            recent_swing_lows=tuple(low_prices),
            liquidity_zones=liquidity_zones,
            volatility_regime=vol_regime,
            calculated_at=datetime.now(timezone.utc),
        )
