"""Immutable market context computed once per symbol/timeframe."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.scanner.types.enums import TrendDirection, VolatilityRegime


@dataclass(frozen=True)
class MarketContext:
    """
    Shared read-only market snapshot for all setup scanners.

    Computed exactly once per symbol/timeframe by MarketContextBuilder.
    Scanners must not recompute these fields — read from context only.
    """

    symbol: str
    timeframe: str
    trend_direction: TrendDirection
    atr_value: float
    key_support_levels: tuple[float, ...]
    key_resistance_levels: tuple[float, ...]
    recent_swing_highs: tuple[float, ...]
    recent_swing_lows: tuple[float, ...]
    liquidity_zones: tuple[tuple[float, str], ...]
    volatility_regime: VolatilityRegime
    calculated_at: datetime
