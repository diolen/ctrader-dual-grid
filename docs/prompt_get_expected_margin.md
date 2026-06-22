# Задача: Реализация get_expected_margin() в Broker Layer

## Цель

Расширить Broker Layer (`app/connection/ctrader_client.py`) методом для оценки маржи через cTrader Open API. Это предварительная задача перед реализацией Trading Layer (Adaptive Dual Grid Portfolio Strategy v8).

## Задача

Добавить метод `get_expected_margin(symbol_id: int, volume_cents: int) -> Optional[float]` в класс `CTraderClient`.

## Требования

1. Использовать `ProtoOAExpectedMarginReq` (payload type = 2139) для запроса оценки маржи
2. Обработать `ProtoOAExpectedMarginRes` (payload type = 2140) в `_listen_loop()`
3. Реализовать future-based паттерн для асинхронного ожидания ответа (аналогично `get_balance()`)
4. Вернуть маржу в валюте депозита (учитывая `moneyDigits` из ответа)
5. Добавить обработку ошибок и таймаутов
6. Написать тест для проверки работы API

## Поля запроса (ProtoOAExpectedMarginReq)

- `ctidTraderAccountId` - ID аккаунта
- `symbolId` - ID символа
- `volume` - объём в центах (repeated field, можно передать одно значение)

## Поля ответа (ProtoOAExpectedMarginRes)

- `margin` - массив `ProtoOAExpectedMargin` с полями:
  - `volume` (int64) - объём в центах для расчёта
  - `buyMargin` (int64) - маржа для BUY
  - `sellMargin` (int64) - маржа для SELL
- `moneyDigits` - экспонента для конвертации monetary значений

## Контекст

Метод должен быть интегрирован в существующую архитектуру `CTraderClient`:
- Использовать существующий механизм отправки сообщений через `_send()`
- Добавить обработчик ответа в `_listen_loop()` для payload type 2140
- Реализовать future-based паттерн для асинхронного ожидания (см. `get_balance()` как пример)
- Следовать существующим паттернам обработки ошибок и таймаутов

## Что нужно на выходе

1. Реализованный метод `get_expected_margin()` в `app/connection/ctrader_client.py`
2. Обработчик `ProtoOAExpectedMarginRes` в `_listen_loop()`
3. Тест для проверки работы API (integration test с demo API)
4. Краткое резюме (в чате, не в коде):
   - Подтверждение работы API с demo аккаунтом
   - Значение маржи для тестового символа и объёма

## Примечание

После реализации этой задачи Trading Layer должен использовать реальную оценку маржи вместо временной заглушки `REFERENCE_LOT_VALUE` в логике `MAX_TOTAL_EXPOSURE` (см. `docs/prompt_dual_grid_v8.md`).
