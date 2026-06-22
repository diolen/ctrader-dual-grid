"""In-memory warmup candle cache to skip redundant trendbar API calls on reconnect."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from app.models.candle import Candle

logger = logging.getLogger(__name__)


def _candle_open_ms(candle: Candle) -> int:
    ts = candle.timestamp
    if isinstance(ts, datetime):
        return int(ts.timestamp() * 1000)
    if ts > 10_000_000_000:
        return int(ts)
    return int(ts) * 1000


@dataclass
class WarmupCacheEntry:
    candles: list[Candle]
    symbol_id: int
    entry_tf: str
    entry_minutes: int
    last_bar_open_ms: int
    min_bars: int


class WarmupCandleCache:
    """Process-local cache survives client reconnect within the same main.py process."""

    def __init__(self) -> None:
        self._entries: dict[str, WarmupCacheEntry] = {}

    def try_get(
        self,
        pair: str,
        *,
        symbol_id: int,
        entry_tf: str,
        entry_minutes: int,
        expected_closed_bar_ms: int,
        min_bars: int,
    ) -> Optional[list[Candle]]:
        entry = self._entries.get(pair)
        if entry is None:
            return None
        if entry.symbol_id != symbol_id or entry.entry_tf != entry_tf:
            return None
        if len(entry.candles) < min(min_bars, entry.min_bars):
            return None
        if entry.last_bar_open_ms < expected_closed_bar_ms:
            return None
        logger.info(
            f"♻️ [{pair}] Warmup cache hit — {len(entry.candles)} bars "
            f"(skip trendbars API)"
        )
        return list(entry.candles)

    def store(
        self,
        pair: str,
        candles: list[Candle],
        *,
        symbol_id: int,
        entry_tf: str,
        entry_minutes: int,
        min_bars: int,
    ) -> None:
        if not candles:
            return
        self._entries[pair] = WarmupCacheEntry(
            candles=list(candles),
            symbol_id=symbol_id,
            entry_tf=entry_tf,
            entry_minutes=entry_minutes,
            last_bar_open_ms=_candle_open_ms(candles[-1]),
            min_bars=min_bars,
        )

    def sync_candles(self, pair: str, candles: list[Candle]) -> None:
        entry = self._entries.get(pair)
        if entry is None or not candles:
            return
        entry.candles = list(candles)
        entry.last_bar_open_ms = _candle_open_ms(candles[-1])

    def invalidate(self, pair: str | None = None) -> None:
        if pair is None:
            self._entries.clear()
        else:
            self._entries.pop(pair, None)


warmup_candle_cache = WarmupCandleCache()
