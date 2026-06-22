# app/models/signal.py
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Signal:
    pair: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    timestamp: datetime
    breakout_price: float
    strategy_type: Optional[str] = "BREAKOUT_RETEST"
    timeframe: Optional[str] = None
    bias_timeframe: Optional[str] = None
    status: SignalStatus = SignalStatus.PENDING
    spread_at_entry: Optional[float] = None
    slippage_pips: Optional[float] = None
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    tp1_size_percent: float = 50.0

    def to_markdown(self) -> str:
        emoji = "🟢 [BUY]" if self.direction == "BUY" else "🔴 [SELL]"
        strategy_info = "**Стратегия:** Breakout Retest Scalping\n"
        timeframe_info = f"**Таймфрейм:** {self.timeframe}\n" if self.timeframe else ""

        base_message = (
            f"{emoji} **SIGNAL ENGINE ALERT**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"**Инструмент:** {self.pair}\n"
            f"**Направление:** {self.direction}\n"
            f"{strategy_info}"
            f"{timeframe_info}"
            f"**Вход (Entry):** {self.entry:.5f}\n"
            f"**Стоп (Stop Loss):** {self.stop_loss:.5f}\n"
            f"**Профит (Take Profit):** {self.take_profit:.5f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"**Время:** {self.timestamp.strftime('%H:%M:%S')} UTC\n"
        )

        extra_info = ""
        if self.spread_at_entry is not None:
            extra_info += f"**Spread:** {self.spread_at_entry:.1f} pips\n"
        if self.slippage_pips is not None:
            extra_info += f"**Slippage:** {self.slippage_pips:.1f} pips\n"
        if self.bias_timeframe:
            extra_info += f"**Bias TF:** {self.bias_timeframe}\n"
        extra_info += "**Фиксация:** Breakout Retest M5"
        return base_message + extra_info

    def to_plain_text(self) -> str:
        return (
            f"Signal Engine [BREAKOUT_RETEST]: {self.direction} {self.pair} "
            f"Entry: {self.entry:.5f}, SL: {self.stop_loss:.5f}, TP: {self.take_profit:.5f}"
        )

    def format_log_line(self, digits: int = 5) -> str:
        spread = (
            f" spread={self.spread_at_entry:.1f}p"
            if self.spread_at_entry is not None
            else ""
        )
        tf = f" {self.timeframe}" if self.timeframe else ""
        if self.strategy_type == "BREAKOUT_RETEST_V3":
            tp_part = "no TP | trailing SL"
        elif self.take_profit_1 > 0:
            tp_part = (
                f"tp={self.take_profit_1:.{digits}f} "
                f"({self.tp1_size_percent:.0f}%)"
            )
        else:
            tp_part = f"tp={self.take_profit:.{digits}f}"
        return (
            f"{self.direction}{tf} entry={self.entry:.{digits}f} "
            f"sl={self.stop_loss:.{digits}f} {tp_part}{spread}"
        )
