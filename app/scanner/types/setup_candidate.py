"""Setup candidate produced by individual scanners."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.scanner.types.enums import Direction, SetupType


@dataclass
class SetupCandidate:
    candidate_id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    timeframe: str = ""
    setup_type: SetupType = SetupType.BREAKOUT
    direction: Direction = Direction.BUY
    score: float = 0.0
    confidence: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    reasons: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ai_explanation: str | None = None

    @property
    def rr_ratio(self) -> float:
        risk = abs(self.entry_price - self.stop_loss)
        if risk <= 0:
            return 0.0
        reward = abs(self.take_profit - self.entry_price)
        return reward / risk
