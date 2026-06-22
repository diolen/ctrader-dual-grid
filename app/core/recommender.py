"""
app/core/recommender.py
Движок интерпретации аналитических данных в текстовые рекомендации для трейдера.
"""
from dataclasses import dataclass
from typing import Optional
from enum import Enum


class MarketPhase(Enum):
    """Фазы рынка для скринера."""
    SCAN_LEVELS = "SCAN_LEVELS"
    DISPLACEMENT_WAIT = "DISPLACEMENT_WAIT"
    WAIT_RETEST = "WAIT_RETEST"
    SETUP_READY = "SETUP_READY"
    SETUP_MISSED = "SETUP_MISSED"


@dataclass
class AnalysisResult:
    """Результат анализа рынка."""
    pair: str
    phase: MarketPhase
    current_price: float
    key_level: Optional[float]
    direction: Optional[str]
    displacement_pips: Optional[float]
    retrace_percent: Optional[float]
    wait_bars: Optional[int]
    timeout_bars: Optional[int]


class MarketRecommender:
    """Преобразует аналитические данные в понятные текстовые рекомендации."""

    @staticmethod
    def phase_to_recommendation(result: AnalysisResult) -> str:
        """Генерирует текстовую рекомендацию на основе фазы рынка."""
        phase = result.phase
        
        if phase == MarketPhase.SCAN_LEVELS:
            return "🔍 Поиск уровней поддержки/сопротивления"
        
        if phase == MarketPhase.DISPLACEMENT_WAIT:
            direction = result.direction or "UNKNOWN"
            level = result.key_level
            if level:
                return f"📈 Пробой {direction} уровня {level:.5f} — ожидание импульса"
            return f"📈 Пробой {direction} — ожидание импульса"
        
        if phase == MarketPhase.WAIT_RETEST:
            direction = result.direction or "UNKNOWN"
            level = result.key_level
            disp = result.displacement_pips or 0
            wait = result.wait_bars or 0
            timeout = result.timeout_bars or 0
            retrace = result.retrace_percent or 0
            
            if level:
                return (
                    f"⏳ Ожидание shallow retest {direction} уровня {level:.5f} | "
                    f"импульс={disp:.1f}p | откат={retrace:.0f}% | wait={wait}/{timeout} бар"
                )
            return f"⏳ Ожидание shallow retest {direction} | импульс={disp:.1f}p"
        
        if phase == MarketPhase.SETUP_READY:
            direction = result.direction or "UNKNOWN"
            level = result.key_level
            entry = result.current_price
            retrace = result.retrace_percent or 0
            
            if level:
                return (
                    f"✅ Сетап сформирован: {direction} зона подтверждения {level:.5f} | "
                    f"откат={retrace:.0f}% | entry={entry:.5f}"
                )
            return f"✅ Сетап сформирован: {direction} | entry={entry:.5f}"
        
        if phase == MarketPhase.SETUP_MISSED:
            direction = result.direction or "UNKNOWN"
            level = result.key_level
            if level:
                return f"❌ Сетуп упущен: {direction} уровень {level:.5f} — откат слишком глубокий"
            return f"❌ Сетап упущен: {direction}"
        
        return f"❓ Неизвестная фаза: {phase.value}"

    @staticmethod
    def format_status_line(result: AnalysisResult) -> str:
        """Форматирует строку статуса для дашборда."""
        phase = result.phase.value
        direction = result.direction or "-"
        level = f"{result.key_level:.5f}" if result.key_level else "-"
        price = f"{result.current_price:.5f}"
        recommendation = MarketRecommender.phase_to_recommendation(result)

        return (
            f"{result.pair:8} | {price:10} | {phase:20} | {direction:4} | "
            f"{level:10} | {recommendation}"
        )
