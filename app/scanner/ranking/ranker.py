"""Setup candidate ranking engine."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timezone

from app.scanner.scanners.common import with_updated_score
from app.scanner.types.enums import Direction, TrendDirection, VolatilityRegime
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate

MAX_RAW_SCORE = 7.0
NORMALIZED_MAX = 10.0

LONDON_NY_START = time(12, 0)
LONDON_NY_END = time(17, 0)


@dataclass
class RankingWeights:
    trend_alignment: float = 2.0
    volatility: float = 1.5
    session_quality: float = 1.0
    rr_ratio: float = 1.5
    pattern_quality: float = 1.0


class SetupRanker:
    """Normalizes and ranks setup candidates on a 0–10 scale."""

    def rank(
        self,
        candidates: list[SetupCandidate],
        context: MarketContext,
        weights: RankingWeights | None = None,
    ) -> list[SetupCandidate]:
        w = weights or RankingWeights()
        ranked: list[SetupCandidate] = []

        for candidate in candidates:
            raw = self._raw_score(candidate, context, w)
            normalized = (raw / MAX_RAW_SCORE) * NORMALIZED_MAX
            ranked.append(with_updated_score(candidate, normalized))

        ranked.sort(key=lambda c: c.score, reverse=True)
        return ranked

    def _raw_score(
        self,
        candidate: SetupCandidate,
        context: MarketContext,
        w: RankingWeights,
    ) -> float:
        score = 0.0

        if self._trend_aligned(candidate, context):
            score += w.trend_alignment

        if context.volatility_regime in (VolatilityRegime.NORMAL, VolatilityRegime.HIGH):
            score += w.volatility

        if self._in_london_ny_session(candidate):
            score += w.session_quality

        if candidate.rr_ratio >= 2.0:
            score += w.rr_ratio

        if candidate.confidence >= 0.8:
            score += w.pattern_quality

        return min(score, MAX_RAW_SCORE)

    @staticmethod
    def _trend_aligned(candidate: SetupCandidate, context: MarketContext) -> bool:
        if context.trend_direction == TrendDirection.RANGING:
            return False
        if candidate.direction == Direction.BUY:
            return context.trend_direction == TrendDirection.BULLISH
        return context.trend_direction == TrendDirection.BEARISH

    @staticmethod
    def _in_london_ny_session(candidate: SetupCandidate) -> bool:
        ts = candidate.timestamp
        if ts.tzinfo is not None:
            t = ts.astimezone(timezone.utc).time()
        else:
            t = ts.time()
        return LONDON_NY_START <= t <= LONDON_NY_END
