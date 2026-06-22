"""Tests for API metrics and warmup cache."""

from datetime import datetime, timedelta, timezone

import pytest

from app.connection.api_metrics import ApiMetrics, is_historical_payload
from app.connection.warmup_cache import WarmupCandleCache
from app.models.candle import Candle


def _bar(i: int, close: float = 1.08) -> Candle:
    base = datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc)
    return Candle(
        timestamp=base + timedelta(minutes=5 * i),
        open=close,
        high=close + 0.001,
        low=close - 0.001,
        close=close,
        volume=100,
    )


class TestApiMetrics:
    def test_historical_payload_type(self):
        assert is_historical_payload(2137) is True
        assert is_historical_payload(2100) is False

    def test_record_send_and_snapshot(self):
        m = ApiMetrics()
        m.record_send(2137)
        m.record_send(2100)
        m.record_trendbar_chunk()
        s = m.snapshot()
        assert s.historical_total == 1
        assert s.non_historical_total == 1
        assert s.trendbar_chunks == 1

    def test_rate_limit_tracking(self):
        m = ApiMetrics()
        m.record_rate_limit(backoff_sec=10.0)
        s = m.snapshot()
        assert s.rate_limit_hits == 1
        assert s.current_backoff_sec == 10.0
        assert s.last_rate_limit_at is not None

    def test_format_summary(self):
        m = ApiMetrics()
        m.record_send(2137)
        m.record_send(2100)
        m.record_trendbar_chunk()
        text = m.format_summary()
        assert "📈 API metrics" in text
        assert "hist=1" in text
        assert "non-hist=1" in text
        assert "chunks=1" in text
        assert "last_rl=never" in text

    def test_log_summary_visible_when_root_is_warning(self, caplog):
        import logging

        m = ApiMetrics()
        m.record_send(2137)
        root = logging.getLogger()
        prev = root.level
        root.setLevel(logging.WARNING)
        try:
            with caplog.at_level(logging.INFO):
                m.log_summary()
        finally:
            root.setLevel(prev)
        assert any("API metrics" in r.message for r in caplog.records)


class TestWarmupCandleCache:
    def test_cache_hit_when_fresh(self):
        cache = WarmupCandleCache()
        candles = [_bar(i) for i in range(150)]
        cache.store(
            "EURUSD",
            candles,
            symbol_id=1,
            entry_tf="M5",
            entry_minutes=5,
            min_bars=150,
        )
        last_ms = int(candles[-1].timestamp.timestamp() * 1000)
        hit = cache.try_get(
            "EURUSD",
            symbol_id=1,
            entry_tf="M5",
            entry_minutes=5,
            expected_closed_bar_ms=last_ms,
            min_bars=150,
        )
        assert hit is not None
        assert len(hit) == 150

    def test_cache_miss_when_stale(self):
        cache = WarmupCandleCache()
        candles = [_bar(i) for i in range(150)]
        cache.store(
            "EURUSD",
            candles,
            symbol_id=1,
            entry_tf="M5",
            entry_minutes=5,
            min_bars=150,
        )
        future_ms = int(candles[-1].timestamp.timestamp() * 1000) + 300_000
        assert cache.try_get(
            "EURUSD",
            symbol_id=1,
            entry_tf="M5",
            entry_minutes=5,
            expected_closed_bar_ms=future_ms,
            min_bars=150,
        ) is None

    def test_sync_updates_last_bar(self):
        cache = WarmupCandleCache()
        candles = [_bar(i) for i in range(10)]
        cache.store(
            "EURUSD",
            candles,
            symbol_id=1,
            entry_tf="M5",
            entry_minutes=5,
            min_bars=10,
        )
        extended = candles + [_bar(10, close=1.09)]
        cache.sync_candles("EURUSD", extended)
        entry = cache._entries["EURUSD"]
        assert len(entry.candles) == 11
