"""Pullback setup scanner — trend + Fibonacci retracement continuation."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.scanner.context.indicators import compute_ema, detect_swing_points
from app.scanner.scanners.common import make_candidate, tp_from_rr
from app.scanner.types.enums import Direction, SetupType, TrendDirection
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate
from app.scanner.utils.candles import signal_bar_index, validate_candles_df

FIB_LOW = 0.382
FIB_HIGH = 0.618
IMPULSE_ATR_MULT = 2.0
DEFAULT_TP_RR = 2.0


@dataclass
class PullbackScanner:
    """
    Trend pullback scanner.

    - Trend: EMA(50) vs EMA(200) — also cross-checks MarketContext.trend_direction
    - Impulse leg range > 2 × ATR
    - Close inside 38.2–61.8% Fib retracement zone
    - Continuation candle closes in trend direction
    """

    ema_fast: int = 50
    ema_slow: int = 200
    impulse_atr_mult: float = IMPULSE_ATR_MULT
    tp_rr: float = DEFAULT_TP_RR
    swing_lookback: int = 60

    def scan(
        self,
        candles: pd.DataFrame,
        context: MarketContext,
    ) -> list[SetupCandidate]:
        validate_candles_df(candles)
        if len(candles) < self.ema_slow + 5 or context.atr_value <= 0:
            return []

        idx = signal_bar_index(candles)
        close = candles["close"]
        ema_fast = compute_ema(close, self.ema_fast)
        ema_slow = compute_ema(close, self.ema_slow)

        if pd.isna(ema_fast.iloc[idx]) or pd.isna(ema_slow.iloc[idx]):
            return []

        bullish_trend = ema_fast.iloc[idx] > ema_slow.iloc[idx]
        bearish_trend = ema_fast.iloc[idx] < ema_slow.iloc[idx]

        if bullish_trend and context.trend_direction == TrendDirection.BEARISH:
            return []
        if bearish_trend and context.trend_direction == TrendDirection.BULLISH:
            return []

        swing_highs, swing_lows = detect_swing_points(
            candles, lookback=self.swing_lookback,
        )
        if not swing_highs or not swing_lows:
            return []

        bar = candles.iloc[idx]
        atr = context.atr_value
        candidates: list[SetupCandidate] = []

        if bullish_trend:
            candidate = self._check_pullback(
                candles,
                context,
                idx,
                bar,
                swing_highs,
                swing_lows,
                direction=Direction.BUY,
                atr=atr,
            )
            if candidate:
                candidates.append(candidate)
        elif bearish_trend:
            candidate = self._check_pullback(
                candles,
                context,
                idx,
                bar,
                swing_highs,
                swing_lows,
                direction=Direction.SELL,
                atr=atr,
            )
            if candidate:
                candidates.append(candidate)

        return candidates

    def _check_pullback(
        self,
        candles: pd.DataFrame,
        context: MarketContext,
        idx: int,
        bar: pd.Series,
        swing_highs: list[tuple[int, float]],
        swing_lows: list[tuple[int, float]],
        *,
        direction: Direction,
        atr: float,
    ) -> SetupCandidate | None:
        if direction == Direction.BUY:
            lows_before = [(i, p) for i, p in swing_lows if i < idx]
            highs_before = [(i, p) for i, p in swing_highs if i < idx]
            if not lows_before or not highs_before:
                return None
            impulse_start_idx, impulse_low = lows_before[-1]
            highs_after = [(i, p) for i, p in highs_before if i > impulse_start_idx]
            if not highs_after:
                return None
            impulse_end_idx, impulse_high = highs_after[-1]
            impulse_range = impulse_high - impulse_low
            if impulse_range <= self.impulse_atr_mult * atr:
                return None

            fib_top = impulse_high - FIB_LOW * impulse_range
            fib_bottom = impulse_high - FIB_HIGH * impulse_range
            close_px = float(bar["close"])
            if not (fib_bottom <= close_px <= fib_top):
                return None
            if float(bar["close"]) <= float(bar["open"]):
                return None

            entry = close_px
            stop_loss = impulse_low - 0.1 * atr
            take_profit = tp_from_rr(entry, stop_loss, direction, self.tp_rr)
            retrace_pct = (impulse_high - close_px) / impulse_range * 100.0

            return make_candidate(
                symbol=context.symbol,
                timeframe=context.timeframe,
                setup_type=SetupType.PULLBACK,
                direction=direction,
                entry=entry,
                stop_loss=stop_loss,
                take_profit=take_profit,
                confidence=min(1.0, 0.55 + impulse_range / (atr * 4)),
                reasons=[
                    f"EMA({self.ema_fast}) > EMA({self.ema_slow}) — bullish trend",
                    f"Impulse leg {impulse_range:.5f} > {self.impulse_atr_mult}×ATR ({atr:.5f})",
                    f"Close in Fib zone 38.2–61.8% (retrace {retrace_pct:.1f}%)",
                    "Bullish continuation candle (close > open)",
                ],
                candles=candles,
                bar_idx=idx,
            )

        highs_before = [(i, p) for i, p in swing_highs if i < idx]
        lows_before = [(i, p) for i, p in swing_lows if i < idx]
        if not highs_before or not lows_before:
            return None
        impulse_start_idx, impulse_high = highs_before[-1]
        lows_after = [(i, p) for i, p in lows_before if i > impulse_start_idx]
        if not lows_after:
            return None
        impulse_end_idx, impulse_low = lows_after[-1]
        impulse_range = impulse_high - impulse_low
        if impulse_range <= self.impulse_atr_mult * atr:
            return None

        fib_bottom = impulse_low + FIB_LOW * impulse_range
        fib_top = impulse_low + FIB_HIGH * impulse_range
        close_px = float(bar["close"])
        if not (fib_bottom <= close_px <= fib_top):
            return None
        if float(bar["close"]) >= float(bar["open"]):
            return None

        entry = close_px
        stop_loss = impulse_high + 0.1 * atr
        take_profit = tp_from_rr(entry, stop_loss, direction, self.tp_rr)
        retrace_pct = (close_px - impulse_low) / impulse_range * 100.0

        return make_candidate(
            symbol=context.symbol,
            timeframe=context.timeframe,
            setup_type=SetupType.PULLBACK,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            confidence=min(1.0, 0.55 + impulse_range / (atr * 4)),
            reasons=[
                f"EMA({self.ema_fast}) < EMA({self.ema_slow}) — bearish trend",
                f"Impulse leg {impulse_range:.5f} > {self.impulse_atr_mult}×ATR ({atr:.5f})",
                f"Close in Fib zone 38.2–61.8% (retrace {retrace_pct:.1f}%)",
                "Bearish continuation candle (close < open)",
            ],
            candles=candles,
            bar_idx=idx,
        )
