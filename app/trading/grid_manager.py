"""Grid Manager for Trading Layer - manages Long and Short grids independently."""

import logging
from typing import Optional, List
from collections import deque

from app.trading.grid_models import GridPosition, Direction


logger = logging.getLogger(__name__)


class GridManager:
    """Manages positions for a single grid (Long or Short)."""
    
    def __init__(self, direction: Direction):
        self.direction = direction
        self.positions: List[GridPosition] = []
    
    def is_active(self) -> bool:
        """Check if grid has any positions."""
        return len(self.positions) > 0
    
    def get_last_position(self) -> Optional[GridPosition]:
        """Get the most recently opened position."""
        if not self.positions:
            return None
        return self.positions[-1]
    
    def total_exposure_lots(self) -> float:
        """Calculate total exposure in lots."""
        return sum(pos.volume for pos in self.positions)
    
    def add_position(self, position: GridPosition) -> None:
        """Add a new position to the grid."""
        self.positions.append(position)
        logger.info(
            f"Grid {self.direction.value}: Added position level {position.level_index}, "
            f"entry={position.entry_price:.5f}, volume={position.volume:.4f} lot",
        )
    
    def remove_position(self, position_id: str) -> Optional[GridPosition]:
        """Remove a position by position_id. Returns removed position or None."""
        for i, pos in enumerate(self.positions):
            if pos.position_id == position_id:
                removed = self.positions.pop(i)
                logger.info(
                    f"Grid {self.direction.value}: Removed position {position_id}, "
                    f"level {removed.level_index}"
                )
                return removed
        return None
    
    def remove_position_by_client_order_id(self, client_order_id: str) -> Optional[GridPosition]:
        """Remove a position by client_order_id. Returns removed position or None."""
        for i, pos in enumerate(self.positions):
            if pos.client_order_id == client_order_id:
                removed = self.positions.pop(i)
                logger.info(
                    f"Grid {self.direction.value}: Removed pending order {client_order_id}, "
                    f"level {removed.level_index}"
                )
                return removed
        return None
    
    def get_position_by_id(self, position_id: str) -> Optional[GridPosition]:
        """Get a position by position_id."""
        for pos in self.positions:
            if pos.position_id == position_id:
                return pos
        return None
    
    def get_position_by_client_order_id(self, client_order_id: str) -> Optional[GridPosition]:
        """Get a position by client_order_id."""
        for pos in self.positions:
            if pos.client_order_id == client_order_id:
                return pos
        return None
    
    def clear_all(self) -> None:
        """Clear all positions."""
        count = len(self.positions)
        self.positions.clear()
        logger.info(f"Grid {self.direction.value}: Cleared {count} positions")
    
    def level_count(self) -> int:
        """Get number of active levels."""
        return len(self.positions)
    
    def can_add_level(self, max_levels: int) -> bool:
        """Check if can add another level."""
        return self.level_count() < max_levels
