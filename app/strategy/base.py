# app/strategy/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime

from app.models.candle import Candle
from app.models.signal import Signal as UnifiedSignal


@dataclass
class MarketData:
    """
    Контейнер для multi-timeframe данных рынка.
    Используется стратегиями для анализа на нескольких таймфреймах.
    """
    pair: str
    # Основной таймфрейм для сигналов
    candles: List[Candle]
    # Дополнительные таймфреймы (например, bias timeframe для scalping)
    extra_timeframes: Dict[str, List[Candle]] = None  # {"M15": [...], "H1": [...]}
    # Флаг прогрева - если True, стратегия не должна генерировать сигналы
    is_warmup: bool = False
    # Спред bid-ask в единицах цены (опционально; для скальпинг-фильтра в стратегии)
    spread: Optional[float] = None

    def __post_init__(self):
        if self.extra_timeframes is None:
            self.extra_timeframes = {}


class BaseStrategy(ABC):
    """
    Абстрактный базовый класс для всех торговых стратегий.
    Определяет общий интерфейс для работы с orchestrator.
    """
    
    @abstractmethod
    async def update(self, market_data: MarketData) -> Optional[UnifiedSignal]:
        """
        Обновляет состояние стратегии на основе новых рыночных данных.
        
        Args:
            market_data: Данные рынка с candles для основного и дополнительных таймфреймов
            
        Returns:
            UnifiedSignal если сигнал сгенерирован, иначе None
        """
        pass
    
    @abstractmethod
    def get_strategy_type(self) -> str:
        """
        Возвращает тип стратегии.
        
        Returns:
            Строка с идентификатором стратегии ("BREAKOUT_RETEST")
        """
        pass
    
    @abstractmethod
    def get_timeframe(self) -> str:
        """
        Возвращает основной таймфрейм стратегии.
        
        Returns:
            Строка с таймфреймом (например, "M5", "M1", "H1")
        """
        pass
