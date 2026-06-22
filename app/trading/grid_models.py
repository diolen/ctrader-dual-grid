"""Data models for Trading Layer - Dual Grid Strategy."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Direction(Enum):
    """Trade direction."""
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass
class GridPosition:
    """Represents a position in the grid."""
    direction: Direction
    entry_price: float
    volume: float
    stop_loss: float
    client_order_id: str
    position_id: Optional[str] = None
    order_placed_at: datetime = field(default_factory=datetime.utcnow)
    position_opened_at: Optional[datetime] = None
    grid_step_at_open: float = 0.0
    level_index: int = 0
    signal_score_at_open: float = 0.0


@dataclass
class ExecutionApproval:
    """Result of execution check."""
    approved: bool
    volume_multiplier: float = 1.0


@dataclass
class DegradationMode:
    """Current degradation mode."""
    NORMAL = "NORMAL"
    CONSERVATIVE = "CONSERVATIVE"
    FREEZE = "FREEZE"
    EXIT = "EXIT"
