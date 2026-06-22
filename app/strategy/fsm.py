# app/strategy/fsm.py
from enum import Enum, auto
from collections import deque
from typing import Optional, Deque, Tuple
from datetime import datetime


class StrategyState(Enum):
    """
    FSM Breakout Retest Scalping (один активный сетап на пару).
    """
    SCAN_LEVELS = auto()
    DISPLACEMENT_WAIT = auto()
    WAIT_RETEST = auto()
    SIGNAL = auto()
    ENTRY_SUBMITTED = auto()
    IN_TRADE = auto()
    CANCELLED = auto()

    # Legacy aliases (совместимость)
    IDLE = SCAN_LEVELS
    IN_POSITION = IN_TRADE


class StateMachine:
    """
    Reusable Finite State Machine для стратегий.
    """

    def __init__(
        self,
        initial_state: StrategyState = StrategyState.SCAN_LEVELS,
        history_size: int = 10,
    ):
        self._current_state = initial_state
        self._history: Deque[Tuple[StrategyState, StrategyState, datetime]] = deque(
            maxlen=history_size,
        )
        self._last_transition_time: Optional[datetime] = None

    @property
    def current_state(self) -> StrategyState:
        return self._current_state

    def transition_to(self, new_state: StrategyState) -> bool:
        if self._current_state == new_state:
            return False

        old_state = self._current_state
        now = datetime.now()
        self._history.append((old_state, new_state, now))
        self._current_state = new_state
        self._last_transition_time = now
        return True

    def get_history(self) -> list[Tuple[StrategyState, StrategyState, datetime]]:
        return list(self._history)

    def time_in_state(self) -> float:
        if self._last_transition_time is None:
            return 0.0
        return (datetime.now() - self._last_transition_time).total_seconds()

    def reset(self, initial_state: StrategyState = StrategyState.SCAN_LEVELS) -> None:
        self._current_state = initial_state
        self._history.clear()
        self._last_transition_time = None
