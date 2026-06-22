"""Future AI explanation layer — interface only, no LLM calls."""
from __future__ import annotations

from typing import Protocol

from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate


class AIExplanationService(Protocol):
    """Reserved for future LLM integration."""

    def explain(
        self,
        candidate: SetupCandidate,
        context: MarketContext,
    ) -> str:
        ...
