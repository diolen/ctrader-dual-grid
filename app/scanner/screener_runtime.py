"""Multi-setup screener runtime — wires context, engine, and scanners."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.models.candle import Candle
from app.scanner.context.builder import MarketContextBuilder
from app.scanner.engine.setup_scanner_engine import SetupScannerEngine
from app.scanner.ranking.ranker import SetupRanker
from app.scanner.scanners.breakout.scanner import BreakoutScanner
from app.scanner.scanners.liquidity_sweep.scanner import LiquiditySweepScanner
from app.scanner.scanners.price_action.scanner import PriceActionScanner
from app.scanner.scanners.pullback.scanner import PullbackScanner
from app.scanner.types.enums import SetupType
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate
from app.scanner.utils.candles import candles_to_df


def _best_candidate_per_symbol(
    candidates: list[SetupCandidate],
) -> list[SetupCandidate]:
    """Keep highest-scored setup per symbol."""
    best: dict[str, SetupCandidate] = {}
    for candidate in candidates:
        prev = best.get(candidate.symbol)
        if prev is None or candidate.score > prev.score:
            best[candidate.symbol] = candidate
    return sorted(best.values(), key=lambda c: c.score, reverse=True)


@dataclass
class MultiSetupScreener:
    """Orchestrates MarketContextBuilder + SetupScannerEngine for all pairs."""

    pair_configs: dict[str, object] = field(default_factory=dict)
    context_builder: MarketContextBuilder = field(default_factory=MarketContextBuilder)
    ranker: SetupRanker = field(default_factory=SetupRanker)
    breakout_scanner: BreakoutScanner = field(default_factory=BreakoutScanner)
    engine: SetupScannerEngine = field(default_factory=SetupScannerEngine)

    def __post_init__(self) -> None:
        self.breakout_scanner.pair_configs = self.pair_configs
        self.engine.scanners = [
            self.breakout_scanner,
            PullbackScanner(),
            LiquiditySweepScanner(),
            PriceActionScanner(),
        ]
        self.engine.ranker = self.ranker

    def set_pip_value(self, pair: str, pip_value: float) -> None:
        self.breakout_scanner.set_pip_value(pair, pip_value)

    def warmup_pair(self, pair: str, candles: list[Candle], entry_tf: str) -> None:
        """Replay history bar-by-bar for BreakoutScanner FSM only (CPU, no API)."""
        self.breakout_scanner.reset(pair)
        df = candles_to_df(candles)
        if len(df) < 5:
            return
        for i in range(5, len(df)):
            slice_df = df.iloc[: i + 1].copy()
            ctx = self.context_builder.build(pair, entry_tf, slice_df)
            self.breakout_scanner.scan(slice_df, ctx)

    def scan_pair(
        self,
        pair: str,
        candles: list[Candle],
        entry_tf: str,
    ) -> tuple[list[SetupCandidate], MarketContext]:
        df = candles_to_df(candles)
        ctx = self.context_builder.build(pair, entry_tf, df)
        candidates = self.engine.scan(df, ctx)

        has_breakout = any(
            c.symbol == pair and c.setup_type == SetupType.BREAKOUT
            for c in candidates
        )
        if not has_breakout:
            snap = self.breakout_scanner.snapshot(df, ctx)
            if snap is not None:
                candidates.append(snap)

        ranked = self.ranker.rank(candidates, ctx) if candidates else []
        return ranked, ctx

    def scan_all_pairs(
        self,
        pair_states: list[tuple[str, list[Candle], str]],
    ) -> list[SetupCandidate]:
        """Scan multiple pairs and return globally ranked candidates."""
        all_candidates: list[SetupCandidate] = []
        contexts: dict[str, MarketContext] = {}

        for pair, candles, entry_tf in pair_states:
            ranked, ctx = self.scan_pair(pair, candles, entry_tf)
            contexts[pair] = ctx
            all_candidates.extend(ranked)

        if not all_candidates:
            return []

        by_symbol: dict[str, list[SetupCandidate]] = {}
        for c in all_candidates:
            by_symbol.setdefault(c.symbol, []).append(c)

        globally_ranked: list[SetupCandidate] = []
        for pair, cands in by_symbol.items():
            ctx = contexts.get(pair)
            if ctx is not None:
                globally_ranked.extend(self.ranker.rank(cands, ctx))

        globally_ranked.sort(key=lambda c: c.score, reverse=True)
        return _best_candidate_per_symbol(globally_ranked)
