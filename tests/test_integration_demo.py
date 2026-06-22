"""
tests/test_integration_demo.py
Интеграционные тесты — требуют реального подключения к cTrader Demo.

Запуск:
    pytest tests/test_integration_demo.py -v -m integration -s

ВАЖНО: убедись что в config.py / .env указаны Demo-credentials,
а не Live! Проверь CTRADER_HOST или аналог.

ПРИМЕЧАНИЕ: тесты требующие живого рынка автоматически пропускаются
в выходные дни (Demo API недоступен Сб–Вс).
"""

import pytest
import asyncio
import logging
import datetime
from types import SimpleNamespace


pytestmark = pytest.mark.integration


# ── Проверка торговых часов ──────────────────────────────────────

def _is_market_open() -> bool:
    """
    Forex рынок открыт Пн 00:00 — Пт 22:00 UTC.
    Demo API Spotware недоступен в выходные.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.weekday() == 6:  # воскресенье
        return False
    if now.weekday() == 5:  # суббота
        return False
    if now.weekday() == 4 and now.hour >= 22:  # пятница после 22:00
        return False
    return True


# Декоратор для тестов требующих живого рынка
market_open = pytest.mark.skipif(
    not _is_market_open(),
    reason="Demo API недоступен в выходные — запусти в Пн–Пт 00:00–22:00 UTC"
)


# ── Фикстуры ────────────────────────────────────────────────────
# scope=function — каждый тест получает свежее соединение.
# Надёжнее чем scope=module: нет проблем с event loop между тестами.

@pytest.fixture
async def demo_client():
    """Реальный клиент cTrader Demo. Свежее соединение для каждого теста."""
    from app.connection.ctrader_client import CTraderClient
    client = CTraderClient()
    await client.connect()
    await client.authorize()
    await client.init_symbols()
    yield client
    await client.disconnect()


@pytest.fixture
async def demo_executor(demo_client):
    from app.trading.executor import TradeExecutor
    return TradeExecutor(demo_client)


# ── Тесты подключения ────────────────────────────────────────────

class TestDemoConnection:

    async def test_client_connects(self, demo_client):
        """Клиент успешно подключился и авторизовался."""
        assert demo_client is not None

    async def test_eurusd_pair_info(self, demo_client):
        """EURUSD инициализирован и содержит нужные поля."""
        info = demo_client.get_pair_info("EURUSD")
        assert info is not None, "EURUSD не найден в символах"
        symbol_id, _, digits, pip_value, _, _ = info
        assert isinstance(symbol_id, int)
        assert digits in (4, 5)
        assert pip_value > 0

    @market_open
    async def test_get_candles(self, demo_client):
        """Загрузка свечей работает и возвращает данные."""
        import time
        import ctrader_open_api.messages.OpenApiModelMessages_pb2 as model_proto

        info = demo_client.get_pair_info("EURUSD")
        symbol_id = info[0]
        now = int(time.time())
        bar_ms = 5 * 60 * 1000

        candles = await demo_client.get_trendbars_chunked(
            symbol_id,
            model_proto.M5,
            (now - 50 * 5 * 60) * 1000,
            (now - 60) * 1000,
            bar_ms=bar_ms,
            pair="EURUSD",
            timeframe="M5",
        )
        assert len(candles) > 0, "Свечи не пришли"
        logging.info(f"Загружено свечей EURUSD M5: {len(candles)}")

    async def test_get_expected_margin(self, demo_client):
        """Получение оценки маржи через API для всех пар."""
        from app.config.settings import config
        pairs = config.PAIRS if config.PAIRS else ["EURUSD"]

        # Тестируем объём 0.01 lot (100_000 cents)
        volume_cents = 100_000

        for pair in pairs:
            info = demo_client.get_pair_info(pair)
            assert info is not None, f"{pair} не найден"
            symbol_id = info[0]

            margin = await demo_client.get_expected_margin(symbol_id, volume_cents)

            assert margin is not None, f"Маржа не получена для {pair}"
            assert isinstance(margin, float), f"Маржа должна быть float для {pair}"
            assert margin > 0, f"Маржа должна быть положительной для {pair}"
            logging.info(f"📊 Expected Margin для {pair} 0.01 lot: {margin:.2f}")


# ── Тесты исполнения ордеров ─────────────────────────────────────

@market_open
class TestDemoExecutor:

    async def test_execute_long_order(self, demo_executor):
        # Проверяем что pair_info есть
        info = demo_executor.client.get_pair_info("EURUSD")
        logging.info(f"pair_info EURUSD: {info}")

        """Отправляет реальный LONG ордер на Demo-счёт."""
        sig = SimpleNamespace(
            timestamp=1748000000000,
            direction="BUY",
            entry=1.08465,
            stop_loss=1.08310,
            take_profit=1.08730,
            strategy_type="SCALPING_MOMENTUM",
            spread_at_entry=None,
        )
        result = await demo_executor.execute(sig, pair="EURUSD")
        logging.info(f"LONG order result: {result}")
        assert result is not None

    async def test_execute_short_order(self, demo_executor):
        """Отправляет SHORT ордер на Demo-счёт."""
        sig = SimpleNamespace(
            timestamp=1748000001000,
            direction="SELL",
            entry=1.08575,
            stop_loss=1.08720,
            take_profit=1.08280,
            strategy_type="SCALPING_MOMENTUM",
            spread_at_entry=None,
        )
        result = await demo_executor.execute(sig, pair="EURUSD")
        logging.info(f"SHORT order result: {result}")
        assert result is not None

    async def test_execute_gbpusd(self, demo_executor):
        """Проверяем что другая пара тоже работает."""
        sig = SimpleNamespace(
            timestamp=1748000002000,
            direction="BUY",
            entry=1.26385,
            stop_loss=1.26240,
            take_profit=1.26700,
            strategy_type="SCALPING_MOMENTUM",
            spread_at_entry=None,
        )
        result = await demo_executor.execute(sig, pair="GBPUSD")
        logging.info(f"GBPUSD order result: {result}")
        assert result is not None