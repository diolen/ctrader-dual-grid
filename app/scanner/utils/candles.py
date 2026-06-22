"""Candle DataFrame helpers for the scanner layer."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.models.candle import Candle

REQUIRED_CANDLE_COLUMNS = frozenset(
    {"open", "high", "low", "close", "volume", "timestamp"},
)


def validate_candles_df(candles: pd.DataFrame) -> None:
    """
    Validate the scanner candle DataFrame contract.

    Contract:
    - Required columns: open, high, low, close, volume, timestamp
    - Rows sorted by timestamp ascending
    - Only closed bars (caller must exclude the forming bar)
    """
    missing = REQUIRED_CANDLE_COLUMNS - set(candles.columns)
    if missing:
        raise ValueError(f"Candles DataFrame missing columns: {sorted(missing)}")

    if len(candles) < 2:
        return

    ts = pd.to_datetime(candles["timestamp"], utc=True)
    if not ts.is_monotonic_increasing:
        raise ValueError("Candles must be sorted by timestamp ascending")


def closed_bars_slice(candles: pd.DataFrame, n: int) -> pd.DataFrame:
    """
    Return the last *n* closed bars.

    When the DataFrame may include a forming bar as the last row, use
    ``candles.iloc[-n-1:-1]``. When the contract guarantees closed-only
    rows, ``candles.iloc[-n:]`` is equivalent — we always apply the safe
    slice when len > n.
    """
    if n <= 0:
        return candles.iloc[0:0]
    if len(candles) <= n:
        return candles.iloc[:-1] if len(candles) > 1 else candles.iloc[0:0]
    return candles.iloc[-n - 1 : -1]


def signal_bar_index(
    candles: pd.DataFrame,
    *,
    forming_bar_may_exist: bool = False,
) -> int:
    """
    Index of the bar to evaluate.

    Scanner contract passes closed bars only → last row (``len - 1``).
    When a forming bar may be appended, use ``len - 2`` (``[-N-1:-1]`` semantics).
    """
    if forming_bar_may_exist and len(candles) > 1:
        return len(candles) - 2
    return max(0, len(candles) - 1)


def df_to_candles(candles: pd.DataFrame) -> list[Candle]:
    """Convert a validated DataFrame to List[Candle] for legacy v3 helpers."""
    rows: list[Candle] = []
    for row in candles.itertuples(index=False):
        ts = row.timestamp
        if not isinstance(ts, datetime):
            ts = pd.Timestamp(ts).to_pydatetime()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        rows.append(
            Candle(
                timestamp=ts,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=int(row.volume),
            )
        )
    return rows


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    """Convert Candle list to scanner DataFrame (closed bars only)."""
    if not candles:
        return pd.DataFrame(columns=list(REQUIRED_CANDLE_COLUMNS))
    return pd.DataFrame(
        {
            "timestamp": [c.timestamp for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
    )
