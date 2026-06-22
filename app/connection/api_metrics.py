"""cTrader Open API request metrics and rate-limit tracking."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_HISTORICAL_PAYLOAD_TYPES = frozenset({2137})  # PROTO_OA_GET_TRENDBARS_REQ


def is_historical_payload(payload_type: int) -> bool:
    return payload_type in _HISTORICAL_PAYLOAD_TYPES


@dataclass
class ApiMetricsSnapshot:
    historical_total: int
    non_historical_total: int
    trendbar_chunks: int
    rate_limit_hits: int
    backoff_events: int
    historical_per_minute: float
    non_historical_per_minute: float
    last_rate_limit_at: datetime | None
    current_backoff_sec: float
    session_started_at: datetime


@dataclass
class ApiMetrics:
    """Thread-safe counters for outbound cTrader Open API traffic."""

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    historical_total: int = 0
    non_historical_total: int = 0
    trendbar_chunks: int = 0
    rate_limit_hits: int = 0
    backoff_events: int = 0
    last_rate_limit_at: datetime | None = None
    current_backoff_sec: float = 0.0
    session_started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    _recent_historical: deque[float] = field(
        default_factory=lambda: deque(maxlen=3600), init=False, repr=False,
    )
    _recent_non_historical: deque[float] = field(
        default_factory=lambda: deque(maxlen=3600), init=False, repr=False,
    )

    def record_send(self, payload_type: int) -> None:
        now = time.monotonic()
        with self._lock:
            if is_historical_payload(payload_type):
                self.historical_total += 1
                self._recent_historical.append(now)
            else:
                self.non_historical_total += 1
                self._recent_non_historical.append(now)

    def record_trendbar_chunk(self) -> None:
        with self._lock:
            self.trendbar_chunks += 1

    def record_rate_limit(self, *, backoff_sec: float) -> None:
        with self._lock:
            self.rate_limit_hits += 1
            self.backoff_events += 1
            self.last_rate_limit_at = datetime.now(timezone.utc)
            self.current_backoff_sec = backoff_sec

    def record_backoff_cleared(self) -> None:
        with self._lock:
            self.current_backoff_sec = 0.0

    def snapshot(self) -> ApiMetricsSnapshot:
        now = time.monotonic()
        window = 60.0
        with self._lock:
            hist_rpm = _count_in_window(self._recent_historical, now, window)
            non_hist_rpm = _count_in_window(self._recent_non_historical, now, window)
            return ApiMetricsSnapshot(
                historical_total=self.historical_total,
                non_historical_total=self.non_historical_total,
                trendbar_chunks=self.trendbar_chunks,
                rate_limit_hits=self.rate_limit_hits,
                backoff_events=self.backoff_events,
                historical_per_minute=hist_rpm,
                non_historical_per_minute=non_hist_rpm,
                last_rate_limit_at=self.last_rate_limit_at,
                current_backoff_sec=self.current_backoff_sec,
                session_started_at=self.session_started_at,
            )

    def format_summary(self) -> str:
        s = self.snapshot()
        last_rl = (
            s.last_rate_limit_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            if s.last_rate_limit_at
            else "never"
        )
        return (
            f"📈 API metrics | hist={s.historical_total} ({s.historical_per_minute:.1f}/min) "
            f"non-hist={s.non_historical_total} ({s.non_historical_per_minute:.1f}/min) "
            f"chunks={s.trendbar_chunks} rate_limits={s.rate_limit_hits} "
            f"backoff={s.current_backoff_sec:.0f}s last_rl={last_rl}"
        )

    def log_summary(self) -> None:
        logger.info(self.format_summary())


def _count_in_window(timestamps: deque[float], now: float, window_sec: float) -> float:
    cutoff = now - window_sec
    while timestamps and timestamps[0] < cutoff:
        timestamps.popleft()
    return float(len(timestamps))
