"""Liquidity sweep setup scanner."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.scanner.scanners.common import make_candidate, tp_from_rr
from app.scanner.types.enums import Direction, SetupType
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate
from app.scanner.utils.candles import signal_bar_index, validate_candles_df

WICK_BODY_MAX_RATIO = 0.40
DEFAULT_TP_RR = 2.0


@dataclass
class LiquiditySweepScanner:
    """
    Detects liquidity sweeps with rejection candles.

    All three conditions required:
    1. Liquidity level (≥2 clustered swing points within 0.5×ATR)
    2. Sweep beyond level + close back inside previous bar range
    3. Wick dominance: body < 40% of full candle range
    """

    tp_rr: float = DEFAULT_TP_RR
    wick_body_max_ratio: float = WICK_BODY_MAX_RATIO

    def scan(
        self,
        candles: pd.DataFrame,
        context: MarketContext,
    ) -> list[SetupCandidate]:
        validate_candles_df(candles)
        if len(candles) < 3 or context.atr_value <= 0:
            return []
        if not context.liquidity_zones:
            return []

        idx = signal_bar_index(candles)
        if idx < 1:
            return []

        bar = candles.iloc[idx]
        prev = candles.iloc[idx - 1]
        prev_low = float(prev["low"])
        prev_high = float(prev["high"])
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])
        full_range = bar_high - bar_low
        body = abs(bar_close - bar_open)

        if full_range <= 0 or body / full_range >= self.wick_body_max_ratio:
            return []

        close_in_prev_range = prev_low <= bar_close <= prev_high
        if not close_in_prev_range:
            return []

        atr = context.atr_value

        for level_price, zone_type in context.liquidity_zones:
            if zone_type == "BUY_SIDE":
                if bar_low >= level_price:
                    continue
                entry = bar_close
                stop_loss = bar_low - 0.25 * atr
                take_profit = tp_from_rr(entry, stop_loss, Direction.BUY, self.tp_rr)
                return [
                    make_candidate(
                        symbol=context.symbol,
                        timeframe=context.timeframe,
                        setup_type=SetupType.LIQUIDITY_SWEEP,
                        direction=Direction.BUY,
                        entry=entry,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        confidence=0.75,
                        reasons=[
                            f"Liquidity cluster at {level_price:.5f} (≥2 swing lows)",
                            f"Sweep below {level_price:.5f}, close back in prior range "
                            f"[{prev_low:.5f}, {prev_high:.5f}]",
                            f"Rejection wick: body {body / full_range:.0%} of range "
                            f"(<{self.wick_body_max_ratio:.0%})",
                        ],
                        candles=candles,
                        bar_idx=idx,
                    )
                ]

            if zone_type == "SELL_SIDE":
                if bar_high <= level_price:
                    continue
                entry = bar_close
                stop_loss = bar_high + 0.25 * atr
                take_profit = tp_from_rr(entry, stop_loss, Direction.SELL, self.tp_rr)
                return [
                    make_candidate(
                        symbol=context.symbol,
                        timeframe=context.timeframe,
                        setup_type=SetupType.LIQUIDITY_SWEEP,
                        direction=Direction.SELL,
                        entry=entry,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        confidence=0.75,
                        reasons=[
                            f"Liquidity cluster at {level_price:.5f} (≥2 swing highs)",
                            f"Sweep above {level_price:.5f}, close back in prior range "
                            f"[{prev_low:.5f}, {prev_high:.5f}]",
                            f"Rejection wick: body {body / full_range:.0%} of range "
                            f"(<{self.wick_body_max_ratio:.0%})",
                        ],
                        candles=candles,
                        bar_idx=idx,
                    )
                ]

        return []
