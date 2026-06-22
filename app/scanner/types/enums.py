"""Shared enumerations for the multi-setup scanner."""
from __future__ import annotations

from enum import Enum, auto


class TrendDirection(Enum):
    BULLISH = auto()
    BEARISH = auto()
    RANGING = auto()


class VolatilityRegime(Enum):
    LOW = auto()
    NORMAL = auto()
    HIGH = auto()


class SetupType(Enum):
    BREAKOUT = auto()
    PULLBACK = auto()
    LIQUIDITY_SWEEP = auto()
    PRICE_ACTION = auto()


class Direction(Enum):
    BUY = auto()
    SELL = auto()


class BreakoutState(Enum):
    """FSM states for BreakoutScanner (screener-only market phases)."""

    IDLE = auto()
    BREAKOUT_DETECTED = auto()
    AWAITING_RETEST = auto()
    CONFIRMED = auto()
    INVALIDATED = auto()
