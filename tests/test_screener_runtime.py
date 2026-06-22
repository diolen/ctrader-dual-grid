"""Tests for MultiSetupScreener runtime helpers."""

from datetime import datetime, timezone

from app.config.settings import _parse_pairs
from app.models.candle import Candle
from app.scanner.screener_runtime import MultiSetupScreener, _best_candidate_per_symbol
from app.scanner.types.enums import Direction, SetupType
from app.scanner.types.setup_candidate import SetupCandidate


def _candidate(symbol: str, setup: SetupType, score: float) -> SetupCandidate:
    return SetupCandidate(
        symbol=symbol,
        timeframe="M5",
        setup_type=setup,
        direction=Direction.SELL,
        score=score,
        entry_price=1.0,
        stop_loss=1.01,
        take_profit=0.98,
    )


class TestParsePairs:
    def test_deduplicates_while_preserving_order(self):
        assert _parse_pairs("EURUSD,GBPUSD,EURUSD,gbpusd") == ["EURUSD", "GBPUSD"]


class TestBestCandidatePerSymbol:
    def test_keeps_highest_score_per_symbol(self):
        candidates = [
            _candidate("EURUSD", SetupType.PRICE_ACTION, 5.7),
            _candidate("EURUSD", SetupType.LIQUIDITY_SWEEP, 4.3),
            _candidate("XAUUSD", SetupType.LIQUIDITY_SWEEP, 4.3),
        ]
        result = _best_candidate_per_symbol(candidates)
        assert [c.symbol for c in result] == ["EURUSD", "XAUUSD"]
        assert result[0].setup_type == SetupType.PRICE_ACTION
        assert result[0].score == 5.7


class TestScanAllPairsDedup:
    def test_returns_one_row_per_symbol(self, monkeypatch):
        screener = MultiSetupScreener()

        def fake_scan_pair(pair, candles, entry_tf):
            return [
                _candidate(pair, SetupType.PRICE_ACTION, 5.7),
                _candidate(pair, SetupType.LIQUIDITY_SWEEP, 4.3),
            ], object()

        monkeypatch.setattr(screener, "scan_pair", fake_scan_pair)
        candles = [
            Candle(
                timestamp=datetime(2024, 6, 3, 10, 0, tzinfo=timezone.utc),
                open=1.0,
                high=1.01,
                low=0.99,
                close=1.0,
                volume=100,
            )
        ]
        result = screener.scan_all_pairs([("EURUSD", candles, "M5")])
        assert len(result) == 1
        assert result[0].symbol == "EURUSD"
        assert result[0].setup_type == SetupType.PRICE_ACTION
