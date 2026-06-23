"""Per-pair Dual Grid registry: long + short grid per symbol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterator, Iterable, Optional, Tuple

from app.trading.grid_manager import GridManager
from app.trading.grid_models import Direction, GridPosition


@dataclass
class PairGrids:
    """Long and short grids for a single trading pair."""

    pair: str
    long_grid: GridManager
    short_grid: GridManager

    def __post_init__(self) -> None:
        if self.long_grid.direction != Direction.LONG:
            raise ValueError(f"{self.pair}: long_grid must be LONG")
        if self.short_grid.direction != Direction.SHORT:
            raise ValueError(f"{self.pair}: short_grid must be SHORT")

    def grid_for(self, direction: Direction) -> GridManager:
        return self.long_grid if direction == Direction.LONG else self.short_grid

    def is_active(self, direction: Direction) -> bool:
        return self.grid_for(direction).level_count() > 0

    def pair_long_lots(self) -> float:
        return self.long_grid.total_exposure_lots()

    def pair_short_lots(self) -> float:
        return self.short_grid.total_exposure_lots()

    def pair_exposure_lots(self) -> float:
        return self.pair_long_lots() + self.pair_short_lots()

    def clear_all(self) -> None:
        self.long_grid.clear_all()
        self.short_grid.clear_all()


class GridBook:
    """Registry of per-pair dual grids."""

    def __init__(self, pairs: Iterable[str]) -> None:
        self._pairs: Dict[str, PairGrids] = {}
        for pair in pairs:
            if pair in self._pairs:
                continue
            self._pairs[pair] = PairGrids(
                pair=pair,
                long_grid=GridManager(Direction.LONG),
                short_grid=GridManager(Direction.SHORT),
            )

    def get(self, pair: str) -> PairGrids:
        if pair not in self._pairs:
            raise KeyError(f"GridBook: pair not registered: {pair}")
        return self._pairs[pair]

    def pairs(self) -> list[str]:
        return list(self._pairs.keys())

    def iter_pair_grids(self) -> Iterator[PairGrids]:
        yield from self._pairs.values()

    def iter_grids(self) -> Iterator[GridManager]:
        for pg in self._pairs.values():
            yield pg.long_grid
            yield pg.short_grid

    def find_position_by_client_order_id(
        self, client_order_id: str,
    ) -> Optional[Tuple[PairGrids, GridPosition]]:
        for pg in self._pairs.values():
            for grid in (pg.long_grid, pg.short_grid):
                pos = grid.get_position_by_client_order_id(client_order_id)
                if pos is not None:
                    return pg, pos
        return None

    def find_position_by_id(
        self, position_id: str,
    ) -> Optional[Tuple[PairGrids, GridPosition]]:
        for pg in self._pairs.values():
            for grid in (pg.long_grid, pg.short_grid):
                pos = grid.get_position_by_id(position_id)
                if pos is not None:
                    return pg, pos
        return None

    def total_exposure_lots(self) -> float:
        return sum(pg.pair_exposure_lots() for pg in self._pairs.values())

    def estimated_total_margin(self, margin_by_pair: Dict[str, float]) -> float:
        """Sum lots × margin_per_lot per pair (MVP global exposure estimate)."""
        total = 0.0
        for pg in self._pairs.values():
            margin = margin_by_pair.get(pg.pair, 0.0)
            if margin <= 0:
                continue
            total += pg.pair_exposure_lots() * margin
        return total

    def clear_all(self) -> None:
        for pg in self._pairs.values():
            pg.clear_all()
