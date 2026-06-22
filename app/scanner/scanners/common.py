"""Shared scanner helpers."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd

from app.scanner.types.enums import Direction, SetupType
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate


def bar_timestamp(candles: pd.DataFrame, idx: int) -> datetime:
    ts = candles.iloc[idx]["timestamp"]
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return pd.Timestamp(ts).to_pydatetime()


def make_candidate(
    *,
    symbol: str,
    timeframe: str,
    setup_type: SetupType,
    direction: Direction,
    entry: float,
    stop_loss: float,
    take_profit: float,
    confidence: float,
    reasons: list[str],
    candles: pd.DataFrame,
    bar_idx: int,
) -> SetupCandidate:
    return SetupCandidate(
        symbol=symbol,
        timeframe=timeframe,
        setup_type=setup_type,
        direction=direction,
        score=confidence * 10.0,
        confidence=confidence,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        reasons=reasons,
        timestamp=bar_timestamp(candles, bar_idx),
        ai_explanation=None,
    )


def tp_from_rr(entry: float, stop_loss: float, direction: Direction, rr: float = 2.0) -> float:
    risk = abs(entry - stop_loss)
    if direction == Direction.BUY:
        return entry + risk * rr
    return entry - risk * rr


def with_updated_score(candidate: SetupCandidate, score: float) -> SetupCandidate:
    return replace(candidate, score=max(0.0, min(10.0, score)))
