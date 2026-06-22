# app/models/candle.py
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class Candle:
    timestamp: datetime  # Время открытия свечи (UTC)
    open: float          # Цена открытия
    high: float          # Максимальная цена
    low: float           # Минимальная цена
    close: float         # Цена закрытия
    volume: int          # Тиковый объем

    @property
    def is_bullish(self) -> bool:
        """Свеча роста (зеленая)"""
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        """Свеча падения (красная)"""
        return self.close < self.open

    @property
    def body_size(self) -> float:
        """Абсолютный размер тела свечи"""
        return abs(self.close - self.open)