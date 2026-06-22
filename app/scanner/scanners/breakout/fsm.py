"""Immutable FSM state and pure transition helpers for BreakoutScanner."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from app.scanner.types.enums import BreakoutState
from app.strategy.breakout_retest_v3 import BreakoutSetup


@dataclass(frozen=True)
class BreakoutFSMState:
    """
    Immutable FSM snapshot for one symbol.

    Transitions return a new instance — never mutate in place.
    """

    state: BreakoutState = BreakoutState.IDLE
    setup: BreakoutSetup | None = None
    last_processed_timestamp: object | None = None


def apply_transition(
    current: BreakoutFSMState,
    new_state: BreakoutState,
    *,
    setup: BreakoutSetup | None = None,
    clear_setup: bool = False,
    last_processed_timestamp: object | None = None,
) -> BreakoutFSMState:
    """Return a new FSM state after a single allowed transition."""
    if current.state == new_state and setup is None and not clear_setup:
        if last_processed_timestamp is not None:
            return replace(current, last_processed_timestamp=last_processed_timestamp)
        return current

    next_setup = None if clear_setup else (setup if setup is not None else current.setup)
    return BreakoutFSMState(
        state=new_state,
        setup=next_setup,
        last_processed_timestamp=(
            last_processed_timestamp
            if last_processed_timestamp is not None
            else current.last_processed_timestamp
        ),
    )


HandlerResult = tuple[BreakoutFSMState, bool]
StateHandler = Callable[..., HandlerResult]

HANDLER_REGISTRY: dict[BreakoutState, str] = {
    BreakoutState.IDLE: "_handle_idle",
    BreakoutState.BREAKOUT_DETECTED: "_handle_breakout_detected",
    BreakoutState.AWAITING_RETEST: "_handle_awaiting_retest",
    BreakoutState.CONFIRMED: "_handle_confirmed",
    BreakoutState.INVALIDATED: "_handle_invalidated",
}
