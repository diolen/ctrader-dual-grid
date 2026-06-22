# app/connection/market_cache.py
import asyncio
import logging
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass


@dataclass(frozen=True)
class QuoteMetrics:
    """
    Метрики котировки для кэширования.
    frozen=True гарантирует неизменяемость после создания.
    """
    bid: float
    ask: float
    spread: float  # в пунктах (pips)
    volume: float
    timestamp: datetime
    
    def is_stale(self, max_age_seconds: float = 5.0) -> bool:
        """
        Проверяет, устарели ли данные.
        
        Args:
            max_age_seconds: Максимальный возраст данных в секундах
            
        Returns:
            True если данные устарели, иначе False
        """
        # 🔥 timezone-aware сравнение
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds() > max_age_seconds


class MarketCache:
    """
    Локальный кэш для spread и volume.
    
    🔥 КРИТИЧЕСКОЕ ТРЕБОВАНИЕ: 
    - get_metrics() — SYNC метод, 0-latency чтение из памяти
    - update_quote() — ASYNC метод с lock для безопасного обновления
    
    Используется для мгновенного доступа к метрикам рынка без API вызовов
    в pre-check executor (scalping hot path).
    """
    
    def __init__(self, max_age_seconds: float = 5.0):
        """
        Args:
            max_age_seconds: Максимальный возраст данных в секундах перед устареванием
        """
        self._lock = asyncio.Lock()
        self._quotes: Dict[str, QuoteMetrics] = {}  # pair -> QuoteMetrics
        self._last_bid_ask: Dict[str, Tuple[float, float]] = {}  # для частичных SpotEvent
        self._pip_values: Dict[str, float] = {}
        self._max_age_seconds = max_age_seconds
    
    # ─────────────────────────────────────────────────────────────
    # 🔥 ASYNC методы (изменение состояния) — требуют lock
    # ─────────────────────────────────────────────────────────────
    
    def set_pip_value(self, pair: str, pip_value: float) -> None:
        """Задаёт pip_value пары (из ProtoOASymbol) для расчёта spread."""
        if pip_value > 0:
            self._pip_values[pair] = pip_value

    def _pip_value_for(self, pair: str) -> float:
        if pair in self._pip_values:
            return self._pip_values[pair]
        pair_upper = pair.upper()
        if any(x in pair_upper for x in ("JPY", "XAU", "XAG", "BTC")):
            return 0.01
        return 0.0001

    async def update_quote(
        self,
        pair: str,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        volume: float = 0.0,
        pip_value: Optional[float] = None,
    ) -> bool:
        """
        Обновляет котировку в кэше. Поддерживает частичные SpotEvent (только bid или только ask).

        Returns:
            True если котировка обновлена, False если данных недостаточно.
        """
        async with self._lock:
            if pip_value is not None and pip_value > 0:
                self._pip_values[pair] = pip_value

            prev_bid, prev_ask = self._last_bid_ask.get(pair, (None, None))
            new_bid = bid if bid is not None and bid > 0 else prev_bid
            new_ask = ask if ask is not None and ask > 0 else prev_ask

            if new_bid is None or new_ask is None or new_ask <= new_bid:
                return False

            self._last_bid_ask[pair] = (new_bid, new_ask)
            pip = self._pip_value_for(pair)
            spread_pips = round((new_ask - new_bid) / pip, 1)

            self._quotes[pair] = QuoteMetrics(
                bid=new_bid,
                ask=new_ask,
                spread=spread_pips,
                volume=volume,
                timestamp=datetime.now(timezone.utc),
            )
            return True
    
    async def clear_stale(self) -> int:
        """
        Удаляет устаревшие данные из кэша.
        
        Returns:
            Количество удаленных записей
        """
        async with self._lock:
            stale_pairs = [
                pair for pair, metrics in self._quotes.items()
                if metrics.is_stale(self._max_age_seconds)
            ]
            
            for pair in stale_pairs:
                del self._quotes[pair]
            
            if stale_pairs:
                logging.info(f"🧹 MarketCache: удалено {len(stale_pairs)} устаревших записей")
            
            return len(stale_pairs)
    
    async def reset(self) -> None:
        """
        Полностью очищает кэш.
        """
        async with self._lock:
            self._quotes.clear()
            self._last_bid_ask.clear()
            logging.info("🔄 MarketCache: кэш очищен")
    
    # ─────────────────────────────────────────────────────────────
    # 🔥 SYNC методы (чтение состояния) — БЕЗ lock, БЕЗ await
    # Критично для 0-latency доступа в executor pre-check
    # ─────────────────────────────────────────────────────────────
    
    def get_metrics(self, pair: str) -> Optional[QuoteMetrics]:
        """
        🔥 SYNC: мгновенное чтение из памяти, без await, без lock.
        
        Args:
            pair: Валютная пара
            
        Returns:
            QuoteMetrics если данные есть и не устарели, иначе None
        """
        metrics = self._quotes.get(pair)
        if metrics is None or metrics.is_stale(self._max_age_seconds):
            return None
        
        return metrics
    
    def get_spread(self, pair: str) -> Optional[float]:
        """
        🔥 SYNC: возвращает spread в пунктах.
        
        Args:
            pair: Валютная пара
            
        Returns:
            Spread в пунктах если данные есть и не устарели, иначе None
        """
        metrics = self.get_metrics(pair)
        return metrics.spread if metrics else None
    
    def get_bid_ask(self, pair: str) -> Optional[Tuple[float, float]]:
        """
        🔥 SYNC: возвращает bid и ask цены.
        
        Args:
            pair: Валютная пара
            
        Returns:
            Кортеж (bid, ask) если данные есть и не устарели, иначе None
        """
        metrics = self.get_metrics(pair)
        return (metrics.bid, metrics.ask) if metrics else None
    
    def is_pair_stale(self, pair: str, max_age_sec: Optional[float] = None) -> bool:
        """
        🔥 SYNC: проверка актуальности для fallback-логики.
        
        Args:
            pair: Валютная пара
            max_age_sec: Переопределение порога устаревания (опционально)
            
        Returns:
            True если данных нет или они устарели
        """
        if max_age_sec is None:
            max_age_sec = self._max_age_seconds
        
        metrics = self._quotes.get(pair)
        if metrics is None:
            return True  # Нет данных = считаем stale
        return metrics.is_stale(max_age_sec)
    
    def get_all_pairs(self) -> list[str]:
        """
        🔥 SYNC: возвращает список всех пар в кэше.
        
        Returns:
            Список валютных пар
        """
        # Копия ключей безопасна без lock в asyncio (GIL + dict.copy() atomic)
        return list(self._quotes.keys())