"""Adapter: legacy AnalysisResult → SetupCandidate (transition period)."""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.recommender import AnalysisResult, MarketPhase
from app.scanner.types.enums import Direction, SetupType
from app.scanner.types.setup_candidate import SetupCandidate


class AnalysisResultAdapter:
    """Converts deprecated AnalysisResult into SetupCandidate."""

    _PHASE_CONFIDENCE = {
        MarketPhase.SCAN_LEVELS: 0.2,
        MarketPhase.DISPLACEMENT_WAIT: 0.5,
        MarketPhase.WAIT_RETEST: 0.65,
        MarketPhase.SETUP_READY: 0.9,
        MarketPhase.SETUP_MISSED: 0.1,
    }

    @classmethod
    def to_candidate(
        cls,
        result: AnalysisResult,
        *,
        timeframe: str = "M5",
    ) -> SetupCandidate:
        direction = cls._parse_direction(result.direction)
        confidence = cls._PHASE_CONFIDENCE.get(result.phase, 0.3)
        entry = result.current_price
        level = result.key_level or entry

        if direction == Direction.BUY:
            stop_loss = level - abs(entry - level) * 0.5 if result.key_level else entry * 0.999
            take_profit = entry + abs(entry - stop_loss) * 2.0
        else:
            stop_loss = level + abs(entry - level) * 0.5 if result.key_level else entry * 1.001
            take_profit = entry - abs(entry - stop_loss) * 2.0

        reasons = [f"Legacy phase: {result.phase.value}"]
        if result.retrace_percent is not None:
            reasons.append(f"Retrace {result.retrace_percent:.0f}%")
        if result.displacement_pips is not None:
            reasons.append(f"Displacement {result.displacement_pips:.1f} pips")

        return SetupCandidate(
            symbol=result.pair,
            timeframe=timeframe,
            setup_type=SetupType.BREAKOUT,
            direction=direction,
            score=confidence * 10.0,
            confidence=confidence,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reasons=reasons,
            timestamp=datetime.now(timezone.utc),
            ai_explanation=None,
        )

    @staticmethod
    def _parse_direction(raw: str | None) -> Direction:
        if raw and raw.upper() == "SELL":
            return Direction.SELL
        return Direction.BUY
