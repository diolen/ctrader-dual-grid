"""Scanner protocol definitions."""
from __future__ import annotations

from typing import Protocol

import pandas as pd

from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate


class SetupScanner(Protocol):
    """
    Universal setup scanner interface.

    Candle DataFrame contract (must be documented and enforced by callers):
    - Required columns: open, high, low, close, volume, timestamp
    - Rows sorted by timestamp ascending
    - Only closed bars — the current forming bar MUST be excluded
    """

    def scan(
        self,
        candles: pd.DataFrame,
        context: MarketContext,
    ) -> list[SetupCandidate]:
        """Scan candles and return zero or more setup candidates."""
        ...
