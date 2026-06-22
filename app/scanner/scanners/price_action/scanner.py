"""Price action pattern scanner — vectorized pandas detection."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.scanner.scanners.common import make_candidate, tp_from_rr
from app.scanner.types.enums import Direction, SetupType
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate
from app.scanner.utils.candles import signal_bar_index, validate_candles_df

DEFAULT_TP_RR = 2.0


@dataclass
class PriceActionScanner:
    """Detects engulfing, pin bar, inside bar, and outside bar patterns."""

    tp_rr: float = DEFAULT_TP_RR
    pin_wick_body_ratio: float = 2.0

    def scan(
        self,
        candles: pd.DataFrame,
        context: MarketContext,
    ) -> list[SetupCandidate]:
        validate_candles_df(candles)
        if len(candles) < 2 or context.atr_value <= 0:
            return []

        idx = signal_bar_index(candles)
        if idx < 1:
            return []

        cur = candles.iloc[idx]
        prev = candles.iloc[idx - 1]

        patterns = self._detect_patterns(cur, prev)
        if not patterns:
            return []

        atr = context.atr_value
        entry = float(cur["close"])
        candidates: list[SetupCandidate] = []

        for pattern_name, direction, reason in patterns:
            if direction == Direction.BUY:
                stop_loss = float(cur["low"]) - 0.25 * atr
            else:
                stop_loss = float(cur["high"]) + 0.25 * atr
            take_profit = tp_from_rr(entry, stop_loss, direction, self.tp_rr)

            candidates.append(
                make_candidate(
                    symbol=context.symbol,
                    timeframe=context.timeframe,
                    setup_type=SetupType.PRICE_ACTION,
                    direction=direction,
                    entry=entry,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    confidence=0.7 if "Inside" in pattern_name else 0.8,
                    reasons=[reason],
                    candles=candles,
                    bar_idx=idx,
                )
            )

        if not candidates:
            return []
        return [max(candidates, key=lambda c: c.confidence)]

    def _detect_patterns(
        self,
        cur: pd.Series,
        prev: pd.Series,
    ) -> list[tuple[str, Direction, str]]:
        o, h, l, c = float(cur["open"]), float(cur["high"]), float(cur["low"]), float(cur["close"])
        po, ph, pl, pc = float(prev["open"]), float(prev["high"]), float(prev["low"]), float(prev["close"])

        cur_body_top = max(o, c)
        cur_body_bot = min(o, c)
        prev_body_top = max(po, pc)
        prev_body_bot = min(po, pc)
        full_range = h - l
        body = abs(c - o)

        found: list[tuple[str, Direction, str]] = []

        # Engulfing — vectorized body comparison on last two rows
        bullish_engulf = (
            c > o and pc < po
            and cur_body_bot <= prev_body_bot
            and cur_body_top >= prev_body_top
            and (cur_body_top - cur_body_bot) > (prev_body_top - prev_body_bot)
        )
        bearish_engulf = (
            c < o and pc > po
            and cur_body_top >= prev_body_top
            and cur_body_bot <= prev_body_bot
            and (cur_body_top - cur_body_bot) > (prev_body_top - prev_body_bot)
        )
        if bullish_engulf:
            found.append((
                "Bullish Engulfing",
                Direction.BUY,
                "Bullish engulfing: current body fully contains previous bearish body",
            ))
        if bearish_engulf:
            found.append((
                "Bearish Engulfing",
                Direction.SELL,
                "Bearish engulfing: current body fully contains previous bullish body",
            ))

        # Inside bar
        if h < ph and l > pl:
            direction = Direction.BUY if c >= o else Direction.SELL
            found.append((
                "Inside Bar",
                direction,
                f"Inside bar: H {h:.5f} < prev H {ph:.5f}, L {l:.5f} > prev L {pl:.5f}",
            ))

        # Outside bar
        if h > ph and l < pl:
            direction = Direction.BUY if c > o else Direction.SELL
            found.append((
                "Outside Bar",
                direction,
                f"Outside bar: H {h:.5f} > prev H {ph:.5f}, L {l:.5f} < prev L {pl:.5f}",
            ))

        # Pin bar — wick > 2× body, body in upper/lower third
        if full_range > 0 and body > 0:
            lower_wick = min(o, c) - l
            upper_wick = h - max(o, c)
            body_position = (cur_body_bot - l) / full_range

            if lower_wick > self.pin_wick_body_ratio * body and body_position >= 2 / 3:
                found.append((
                    "Bullish Pin Bar",
                    Direction.BUY,
                    f"Pin bar: lower wick {lower_wick:.5f} > 2× body {body:.5f}, "
                    f"body in lower third (pos={body_position:.2f})",
                ))
            if upper_wick > self.pin_wick_body_ratio * body and body_position <= 1 / 3:
                found.append((
                    "Bearish Pin Bar",
                    Direction.SELL,
                    f"Pin bar: upper wick {upper_wick:.5f} > 2× body {body:.5f}, "
                    f"body in upper third (pos={body_position:.2f})",
                ))

        return found
