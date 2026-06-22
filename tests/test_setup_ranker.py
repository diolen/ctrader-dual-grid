"""Unit tests for SetupRanker."""

from datetime import datetime, timezone

import pytest

from app.scanner.ranking.ranker import RankingWeights, SetupRanker
from app.scanner.types.enums import Direction, SetupType, TrendDirection, VolatilityRegime
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate


def _mock_context(
    trend: TrendDirection = TrendDirection.BULLISH,
    vol: VolatilityRegime = VolatilityRegime.NORMAL,
) -> MarketContext:
    return MarketContext(
        symbol="EURUSD",
        timeframe="M5",
        trend_direction=trend,
        atr_value=0.0010,
        key_support_levels=(1.0800,),
        key_resistance_levels=(1.0900,),
        recent_swing_highs=(1.0900,),
        recent_swing_lows=(1.0800,),
        liquidity_zones=((1.0800, "BUY_SIDE"),),
        volatility_regime=vol,
        calculated_at=datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc),
    )


def _mock_candidate(
    *,
    score: float = 0.0,
    direction: Direction = Direction.BUY,
    confidence: float = 0.85,
    entry: float = 1.0850,
    sl: float = 1.0820,
    tp: float = 1.0910,
    ts: datetime | None = None,
) -> SetupCandidate:
    return SetupCandidate(
        symbol="EURUSD",
        timeframe="M5",
        setup_type=SetupType.BREAKOUT,
        direction=direction,
        score=score,
        confidence=confidence,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        reasons=["test"],
        timestamp=ts or datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc),
    )


class TestSetupRanker:
    def test_rank_normalizes_to_0_10(self):
        ranker = SetupRanker()
        ctx = _mock_context()
        candidate = _mock_candidate()

        ranked = ranker.rank([candidate], ctx)

        assert len(ranked) == 1
        assert 0.0 <= ranked[0].score <= 10.0
        # trend(2) + vol(1.5) + session(1) + rr(1.5) + pattern(1) = 7 → 10.0
        assert ranked[0].score == pytest.approx(10.0)

    def test_rank_sorts_descending(self):
        ranker = SetupRanker()
        ctx = _mock_context()

        low = _mock_candidate(confidence=0.5)
        high = _mock_candidate(confidence=0.9)

        ranked = ranker.rank([low, high], ctx)
        assert ranked[0].score >= ranked[1].score

    def test_trend_mismatch_reduces_score(self):
        ranker = SetupRanker()
        ctx = _mock_context(trend=TrendDirection.BEARISH)
        candidate = _mock_candidate(direction=Direction.BUY)

        ranked = ranker.rank([candidate], ctx)
        # Missing trend_alignment (2.0) → raw 5.0 → ~7.14
        assert ranked[0].score == pytest.approx(5.0 / 7.0 * 10.0, rel=0.01)

    def test_custom_weights(self):
        ranker = SetupRanker()
        ctx = _mock_context()
        weights = RankingWeights(
            trend_alignment=1.0,
            volatility=0.0,
            session_quality=0.0,
            rr_ratio=0.0,
            pattern_quality=0.0,
        )
        candidate = _mock_candidate()

        ranked = ranker.rank([candidate], ctx, weights)
        assert ranked[0].score == pytest.approx(1.0 / 7.0 * 10.0, rel=0.01)

    def test_empty_candidates(self):
        ranker = SetupRanker()
        assert ranker.rank([], _mock_context()) == []
