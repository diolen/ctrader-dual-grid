"""Unit-тесты логики опроса свечей (main.py helpers)."""

from datetime import datetime, timezone

from app.models.candle import Candle
from main import (
    _expected_closed_bar_open_ms,
    _filter_bars_up_to,
    _merge_new_candles,
)


def _ts_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class TestExpectedClosedBar:

    def test_m1_at_four_seconds_past_minute(self):
        now = int(datetime(2026, 6, 3, 22, 9, 4, tzinfo=timezone.utc).timestamp())
        expected = _expected_closed_bar_open_ms(now, 1)
        assert expected == _ts_ms(datetime(2026, 6, 3, 22, 8, 0, tzinfo=timezone.utc))

    def test_m15_on_boundary(self):
        now = int(datetime(2026, 6, 3, 22, 15, 4, tzinfo=timezone.utc).timestamp())
        expected = _expected_closed_bar_open_ms(now, 15)
        assert expected == _ts_ms(datetime(2026, 6, 3, 22, 0, 0, tzinfo=timezone.utc))


class TestFilterBars:

    def test_drops_future_bar(self):
        expected_ms = _ts_ms(datetime(2026, 6, 3, 22, 8, 0, tzinfo=timezone.utc))
        bars = [
            Candle(expected_ms, 1.0, 1.1, 0.9, 1.05, 100),
            Candle(
                _ts_ms(datetime(2026, 6, 3, 22, 9, 0, tzinfo=timezone.utc)),
                1.0, 1.1, 0.9, 1.05, 100,
            ),
        ]
        filtered = _filter_bars_up_to(bars, expected_ms)
        assert len(filtered) == 1
        assert _merge_new_candles([], filtered) == 1
