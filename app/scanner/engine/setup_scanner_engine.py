"""Multi-scanner orchestration engine."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.scanner.protocols.setup_scanner import SetupScanner
from app.scanner.ranking.ranker import RankingWeights, SetupRanker
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate


@dataclass
class SetupScannerEngine:
    """
    Iterates registered scanners, collects candidates, ranks them.

    Open/closed: add scanners without modifying engine logic.
    """

    scanners: list[SetupScanner] = field(default_factory=list)
    ranker: SetupRanker = field(default_factory=SetupRanker)
    ranking_weights: RankingWeights | None = None

    def register(self, scanner: SetupScanner) -> None:
        self.scanners.append(scanner)

    def scan(
        self,
        candles: pd.DataFrame,
        context: MarketContext,
    ) -> list[SetupCandidate]:
        candidates: list[SetupCandidate] = []
        for scanner in self.scanners:
            candidates.extend(scanner.scan(candles, context))
        if not candidates:
            return []
        return self.ranker.rank(candidates, context, self.ranking_weights)
