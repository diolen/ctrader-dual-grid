# Задача: Adaptive Dual Grid Portfolio Strategy v8 (Trading Layer)

Реализуй Trading Layer для форекс-системы поверх уже существующих в этом репозитории
Broker Layer и Scanner Layer. НЕ переписывай Broker Layer и Scanner Layer — используй
их существующие классы и методы как есть.

**Предварительное требование:** Перед реализацией этой задачи должен быть реализован метод
`get_expected_margin()` в Broker Layer (см. `docs/prompt_get_expected_margin.md`). Trading Layer
будет использовать этот метод для оценки маржи вместо временной заглушки `REFERENCE_LOT_VALUE`.

Эта версия — рабочий, тестируемый каркас. Все условия ниже выражены как конкретные,
проверяемые правила. Решения, требующие проверки на реальном исполнении, помечены
ДОПУЩЕНИЕ. Открытые вопросы, на которые в этом промпте сознательно не дан ответ
(потому что ответ зависит от деталей кода, которые нужно сверить, а не угадать),
помечены ТРЕБУЕТ ПРОВЕРКИ — по ним агент должен остановиться и спросить, прежде
чем писать соответствующую часть кода.

## Архитектурное примечание: Bar-based vs Tick-based

Существующая система в `main.py` использует **bar-based** подход: опрос свечей через
`_poll_pair()` → `_fetch_latest_bars()` → `_run_strategy_tick()`. Trading Layer должен
интегрироваться в эту архитектуру, а не заменять её на tick-based `on_tick()`.

**Критическое архитектурное решение:** Trading Layer **интегрируется с** существующим
`StrategyOrchestrator`, а не заменяет его. `StrategyOrchestrator` выполняет критичные
функции, которые нельзя потерять:
- Получение `pip_value` из `get_pair_info()` (нужно для расчёта объёма позиции)
- Установка `pair_config` для стратегии
- Error handling и логирование
- Отслеживание pending-ордеров (`_limit_orders_by_pair`)
- TradeGuard интеграция
- Recovery и reconciliation логика

Trading Layer использует `SetupScannerEngine.scan()` для получения сигналов, но
сохраняет интеграцию с `StrategyOrchestrator` для:
- Получения `pip_value` через `orchestrator._client.get_pair_info(pair)`
- **Открытия позиций:** после успешного `place_limit_order()` обязательно вызывать
  `orchestrator.track_limit_order(pair, client_order_id)` для отслеживания
  pending-ордера в `_limit_orders_by_pair` (нужно для TradeGuard, recovery,
  reconciliation logic). Это НЕ дублирование callback — `on_execution_event`
  обрабатывает только `ORDER_FILLED`, `ORDER_CANCELLED`, `ORDER_EXPIRED`,
  `ORDER_REJECTED` и `is_position_close`, но НЕ обрабатывает момент выставления
  ордера (`ORDER_ACCEPTED`). Без явного `track_limit_order()` orchestrator не
  узнает о pending-ордере до его исполнения/отмены.

  **Критический edge case:** если `place_limit_order()` вернул валидный
  `client_order_id`, но `orchestrator.track_limit_order()` упал с исключением,
  ордер реально выставлен у брокера, но orchestrator о нём не знает.
  `reconcile_missing_broker_pending()` НЕ восстанавливает такие пропущенные записи
  (он только удаляет несоответствия, не добавляет отсутствующие). Trading Layer
  должен:
  - Обернуть `track_limit_order()` в try/except
  - При исключении логировать как `logger.critical()` и НЕ считать уровень открытым
  - В TTL-логике проверять `get_pending_orders()` у брокера перед попыткой
    повторного открытия, чтобы избежать дублирования объёма
- **Закрытия позиций:** использовать `orchestrator.cancel_tracked_limit_order()`
  для отмены pending-ордеров (если есть), но для закрытия уже открытых позиций
  можно вызывать `close_position_partial()` напрямую — orchestrator узнает
  через `on_execution_event` callback (is_position_close=True). Эта асимметрия
  оправдана: закрытие позиции всегда генерирует callback, а выставление
  pending-ордера — нет.
- **Recovery логики:** использовать `orchestrator.run_recovery()` для восстановления
  состояния после обрыва соединения

Сигнатура метода:
```
on_bar_update(
    state: _PairRuntime,
    orchestrator: StrategyOrchestrator,
    client: CTraderClient,
    market_cache: MarketCache,
) -> None
```

`_PairRuntime` содержит: pair, symbol_id, candles (List[Candle]), entry_tf,
entry_period, entry_minutes. Метод вызывается для **одного конкретного символа**
(не перебирает все `watched_instruments` — перебор происходит на уровне
`main.py` в цикле по парам). Метод вызывается на каждом новом баре для
конкретного символа.

**Точка интеграции с main.py:** в `_run_strategy_tick()` (или эквивалентном методе
`main.py`) после `_fetch_latest_bars()` вызывается `trading_engine.on_bar_update(state,
orchestrator, client, market_cache)` для каждой пары.

## Внутреннее состояние TradingEngine

`TradingEngine` хранит собственное состояние сверх того, что предоставляют
Broker/Scanner Layer:

- `watched_instruments: list[(symbol, timeframe)]` — список наблюдаемых пар и
  таймфреймов, задаётся в конфигурации при инициализации. Broker/Scanner Layer
  не сообщают, что сканировать — это явно знает только Trading Layer.
- `spread_history: dict[symbol, RollingWindow[float]]` — скользящее окно
  последних `SPREAD_LOOKBACK_BARS` значений спреда **на размер этого окна**,
  заполняемое самим `TradingEngine` на каждом цикле через `get_spread(pair)`.
  Broker Layer не хранит историю спредов — это собственное состояние Trading
  Layer (см. ДОПУЩЕНИЕ ниже).
- `atr_baseline_history: dict[symbol, RollingWindow[float]]` — аналогично, окно
  значений ATR для расчёта `ATR_baseline`, размер `ATR_BASELINE_LOOKBACK_BARS`.
- `pending_order_metadata: dict[client_order_id, dict]` — метадата для всех
  выставленных грид-ордеров, используемая в reconciliation. Структура:
  `{'level_index': int, 'grid_step_at_open': float, 'signal_score_at_open': float,
  'order_placed_at': datetime, 'direction': Direction}`. Заполняется при
  успешном `place_limit_order()`, удаляется при исполнении/отмене.
- `consecutive_execution_rejections: int` — единый глобальный счётчик (см. раздел
  про режимы деградации — система рассчитана на один торгуемый символ в текущей
  версии; если потребуется мультисимвольная торговля с раздельными счётчиками
  деградации на каждый символ — это отдельная задача, не реализовывать сейчас
  по умолчанию, без явного запроса).
- `halted: bool` — состояние после глобального TP/SL до `restart()`.

**ДОПУЩЕНИЕ — ведение истории спредов и ATR:** так как Broker Layer не
предоставляет историю спредов, `TradingEngine` обязан сам накапливать
`spread_history` на каждом цикле, добавляя текущее значение `get_spread(pair)` в
конец окна и вытесняя самое старое при превышении размера окна. До тех пор пока
накоплено меньше `SPREAD_LOOKBACK_BARS` значений (например, сразу после
запуска), execution-проверка по спреду пропускается (`approved = True` по этому
конкретному условию, не блокирует), чтобы не давать ложных отказов на холодном
старте. То же правило применяется к `atr_baseline_history`.

**Важно:** история спреда и ATR обновляется на каждом цикле независимо от текущего
режима деградации, включая Exit и Freeze. Это необходимо для корректного
определения момента выхода из этих режимов обратно в Normal.

## Жизненный цикл одного обновления (`on_bar_update`)

Порядок шагов фиксирован, не оставлять на свободную реализацию агента.
Метод вызывается для **одного конкретного символа** (параметр `state: _PairRuntime`),
не перебирает `watched_instruments` — перебор происходит на уровне `main.py` в
цикле по парам.

0. **Reconciliation с брокером (критический шаг):** синхронизировать
   `GridManager.positions` с реальным состоянием через `client.get_positions(force=False)`.
   - Удалить из памяти позиции, которых нет у брокера (закрылись по SL/TP вручную)
   - Для позиций, которые есть у брокера, но не в памяти:
     - **Алгоритм сопоставления с `pending_order_metadata`:** для каждой позиции брокера
       без соответствующего `GridPosition` в памяти — искать в `pending_order_metadata` запись
       с тем же `direction` и `entry_price` в пределах допуска (разница меньше половины текущего
       пункта спреда), выставленную позже последнего успешного reconciliation. При множественных
       совпадениях — это аномалия, обрабатывать как несопоставленную (warning), не угадывать.
     - Если найдено соответствие в `pending_order_metadata` → **автоматически добавить** в
       `GridManager` с пометкой "восстановлена из broker state после исполнения pending".
       Все поля `GridPosition` (`level_index`, `grid_step_at_open`, `signal_score_at_open`,
       `position_opened_at`) берутся из метадаты `pending_order_metadata` по найденному
       `client_order_id`. `position_id` устанавливается из `get_positions()`, `order_placed_at`
       копируется в `position_opened_at`. После успешного восстановления — удалить
       соответствующую запись из `pending_order_metadata`. (штатная ситуация при исполнении между циклами)
     - Если нет соответствия в `pending_order_metadata` → **warning** о рассинхронизации
       (реальная аномалия, не пытаться автоматически добавить)
   - Без этого шага `grid_step_at_open`, `level_index` и другие поля `GridPosition`
     теряют смысл, так как могут ссылаться на несуществующие позиции

0.5. **TTL-проверка pending грид-ордеров:** для всех pending ордеров текущей пары,
   чьи `client_order_id` есть в `pending_order_metadata`, проверить TTL:
   `(current_bar.timestamp.timestamp() * 1000 - order_placed_at.timestamp() * 1000) > GRID_ORDER_TTL_BARS * bar_duration`.
   Если TTL истёк — отменить через `cancel_order_by_client_id()` и удалить запись
   из `pending_order_metadata`. Эта проверка выполняется ДО обработки нового сигнала.

1. Получить текущую цену/спред для `state.pair` через `get_spread(pair)`,
   где `pair = state.pair` (имя символа напрямую используется как pair для вызова
   брокерского метода). Добавить значение спреда в `spread_history[state.pair]`.
   **Оценка маржи:** если `margin_per_lot` ещё не кэширован (первый запуск или после
   `restart()`), вызвать `client.get_expected_margin(symbol_id, volume_cents=100_000)`
   для объёма 1 лота (100_000 cents), получить маржу и кэшировать как `margin_per_lot`.
   Если `get_expected_margin()` вернул `None` — логировать `critical`, `can_expand()`
   возвращает `False` до успешного кэширования на следующем цикле.
   Кэш `margin_per_lot` сохраняется до следующего `restart()` — нет смысла дёргать
   брокера каждый цикл, так как плечо/тип маржи меняются редко.
2. Конвертировать `state.candles: List[Candle]` в `pd.DataFrame` (по полям
   timestamp, open, high, low, close, volume). Рассчитать `current_ATR` и
   `ATR_baseline` из этого DataFrame через `compute_atr()`. Добавить значение
   в `atr_baseline_history[state.pair]`.
   **Важно:** НЕ вызывать `get_trendbars_chunked()` внутри Trading Layer — свечи
   уже получены в `main.py` и переданы через `state.candles`. Дублирующий запрос к
   брокеру за теми же свечами приведёт к лишней нагрузке и потенциальной
   рассинхронизации.
3. Если `halted == True` — выйти, не выполнять остальные шаги.
4. Проверить глобальные TP/SL (раздел 8) на основе текущего PnL портфеля. Если
   сработало — выполнить закрытие, установить `halted = True`, выйти.
5. Определить текущий режим деградации (Normal/Conservative/Freeze/Exit, раздел 8).
   Если Exit — выполнить `close_worst_position()` и выйти (новые сигналы в этом
   цикле не обрабатываются).
6. Построить `MarketContext` через `MarketContextBuilder.build(state.pair, state.entry_tf, candles_df)`,
   где `candles_df` — DataFrame из шага 2.
7. Вызвать `SetupScannerEngine.scan(candles_df, context)`, получить список
   `SetupCandidate`.
   **Важно:** Trading Layer вызывает сканер напрямую для получения сигналов,
   но использует `orchestrator._client.get_pair_info(state.pair)` для получения
   `pip_value` (нужно для расчёта объёма позиции). Не использовать
   `orchestrator.update()` или модель `Signal` из существующей системы для
   получения сигналов, но сохранять интеграцию с orchestrator для управления
   ордерами и recovery логики.

   **Обработка пустого результата:** если `scan()` вернул пустой список — это
   штатная ситуация (рынок тихий, нет сетапов). Никаких действий не выполняется:
   уровни не открываются, счётчики не меняются, режим деградации не переключается.
8. **Обработка сигналов:** `scan()` возвращает отсортированный список всех кандидатов
   (по убыванию `score`). Trading Layer берёт первый кандидат из списка
   (максимальный `score`), остальные кандидаты этого цикла игнорируются.
   **Tie-break при равном score:** если несколько кандидатов имеют одинаковый
   максимальный `score`, поведение не определяется этим документом — сортировка
   Scanner Layer считается источником истины.
9. Выполнить маппинг `BUY/SELL → LONG/SHORT`, применить логику Long/Short Grid
   Manager (открытие первой позиции / добавления уровня — разделы 2 и 4),
   используя режим деградации (шаг 5) и `can_expand()` (раздел 7) как фильтры.
10. Для каждого решения "открыть позицию" — выполнить execution-проверку
    (раздел 6) непосредственно перед отправкой ордера, скорректировать объём
    или отказать, обновить `consecutive_execution_rejections`.

    **Критическая проверка перед отправкой ордера:** перед любым новым решением
    открыть/добавить уровень — проверить `get_pending_orders(force=True)` у брокера
    на предмет уже существующего неотслеживаемого ордера для этой сетки/уровня
    (сравнение по `client_order_id` из `pending_order_metadata`). Если такой ордер
    найден — НЕ отправлять новый ордер, логировать как warning и считать уровень
    открытым (добавить в `GridPosition` с `position_id=None`). Это предотвращает
    дублирование объёма при сбое `track_limit_order()`.

    **Обработка `place_limit_order` → `None`:** если `place_limit_order` вернул
    `None` — это считается execution-отказом. `consecutive_execution_rejections`
    инкрементируется, позиция не добавляется в `GridPosition`, на следующем цикле
    условия открытия/добавления уровня проверяются заново (без автоматического retry
    того же ордера).

## Точные интерфейсы существующих слоёв

### Broker Layer

Реализован в классе `CTraderClient` (файл `app/connection/ctrader_client.py`).

**Котировки:**
- `subscribe_quotes(market_cache)` — вызывается один раз при инициализации
  `TradingEngine`, до первого `on_bar_update()`.
- `get_spread(pair)` — текущий спред в пунктах (моментальный снимок, без
  истории — история ведётся в `TradingEngine`, см. выше).

**Исторические свечи:**
- `get_trendbars_chunked(symbol_id, period, from_timestamp, to_timestamp, bar_ms, chunk_bars, pair, timeframe, verbose)` →
  `List[Candle]`.

**Открытие ордеров:**
- `place_limit_order(symbol_id, direction, lot, entry, stop_loss, multiplier, min_volume, step_volume, pair)` →
  `(client_order_id, ResolvedVolume)` или `None`.
- `place_stop_order(...)` — не используется для входов грида в этой версии
  (используется только `place_limit_order`, см. допущение ниже).
  **Важно:** параметр `multiplier` в обеих функциях НЕ ИСПОЛЬЗУЕТСЯ в теле функции
  (проверено в ctrader_client.py строки 390-466 и 467-516). Передавать любое
  значение (например, `1`), параметр оставлен для совместимости, но не влияет
  на логику.

**ДОПУЩЕНИЕ — модель исполнения грид-уровней:** входы реализуются через
`place_limit_order` с `entry = current_price` на момент решения, как
практический эквивалент рыночного входа. Дополнительно: каждый такой
лимит-ордер получает TTL `GRID_ORDER_TTL_BARS` (новый конфигурируемый
параметр). Если по истечении `GRID_ORDER_TTL_BARS` циклов ордер остаётся в
`get_pending_orders()` неисполненным, он отменяется через
`cancel_order_by_client_id()`. Повторная попытка выставить уровень происходит
только при повторном выполнении всех условий открытия/добавления уровня на
следующем цикле (не автоматический re-submit того же ордера) — то есть
истечение TTL не запускает retry-логику само по себе, оно просто снимает
зависший ордер и возвращает сетку в состояние "уровень не открыт", откуда
обычные условия могут (или не могут) сработать заново.

**Закрытие ордеров и позиций:**
- `cancel_order(broker_order_id, timeout)` / `cancel_order_by_client_id(client_order_id, timeout)`.
- `close_position_partial(position_id, volume_cents, timeout)`.

**ДОПУЩЕНИЕ — полное закрытие позиции:** реализуется через
`close_position_partial(position_id, volume_cents=<полный объём позиции>)`.
Это допущение применяется только к `GridPosition` с известным `position_id`.
Для записей с `position_id=None` (pending ордера) используется отдельная логика
отмены через `cancel_order_by_client_id()` (см. Exit-режим и глобальный TP/SL выше).

После каждого вызова `close_all_positions()` или `close_worst_position()`
обязательно выполняется повторный запрос `get_positions(force=True)`, чтобы
подтвердить, что позиция действительно закрыта (исчезла из списка или её
объём стал 0). Если позиция всё ещё присутствует с прежним объёмом — это
логируется как критическая ошибка (`logger.critical`), и повторная попытка
закрытия выполняется один раз; если и она не помогает — система должна
оставаться в `halted` состоянии и не предпринимать новых попыток без
вмешательства оператора (не уходить в бесконечный retry-цикл).

- `amend_position_sltp(...)` — не используется для закрытия.

**Список открытых позиций:**
- `get_positions(force=False)` — dict с полями: id, symbol, volume, entry_price,
  current_price, margin, profit, direction.
- `get_pending_orders(force=False)`, `get_reconcile_state(force=False)`.

**ДОПУЩЕНИЕ — метрика PnL отдельной позиции:** поле `profit` из `get_positions()`
используется как есть в качестве unrealized PnL в валюте депозита, без
дополнительной конвертации. Это применяется для выбора "наиболее убыточной"
позиции в Exit-режиме и для расчёта суммарного PnL портфеля.

**Equity:** `get_balance()`.

**Execution-метрики:** нет latency/re-quote данных. Execution-проверка строится
на `get_spread()` и собственной истории спредов/ATR, ведущейся в `TradingEngine`.

### Scanner Layer

`SetupScannerEngine.scan(candles: pd.DataFrame, context: MarketContext) -> list[SetupCandidate]`
(файл `app/scanner/engine/setup_scanner_engine.py`).

**Конвертер свечей:** Свечи уже получены в `main.py` через `_fetch_latest_bars()` и
передаются в `on_bar_update()` как `List[Candle]`. Trading Layer конвертирует
`List[Candle]` → `pd.DataFrame` по полям timestamp, open, high, low, close, volume.
Поля `Candle` подтверждены: timestamp (datetime), open (float), high (float),
low (float), close (float), volume (int).

**`MarketContextBuilder`** (файл `app/scanner/context/builder.py`):
`MarketContextBuilder.build(symbol, timeframe, candles: pd.DataFrame) -> MarketContext`.
`symbol`/`timeframe` для вызова берутся из параметров `on_bar_update(symbol, timeframe, candles)`
— не из `watched_instruments` и не из `SetupCandidate`, так как `MarketContext`
строится ДО получения кандидатов.

**Объект сигнала (`SetupCandidate`):** `direction: Direction` (**BUY/SELL**, не
LONG/SHORT — маппинг `BUY → LONG`, `SELL → SHORT` выполняется на границе внутри
`TradingEngine`). Поля `stop_loss`/`take_profit`/`rr_ratio` сигнала НЕ
используются для управления грид-позицией (грид использует собственный
ATR-based SL) — допустимо использовать их только как метаданные для логирования.

## Модель данных: `GridPosition`

Помимо стандартных полей (направление, цена входа, объём, SL), `GridPosition`
обязательно хранит:
- `client_order_id: str` — для возможности отмены через `cancel_order_by_client_id()`;
- `position_id: str | None` — ID позиции у брокера, заполняется при исполнении ордера.
  Пока `None` (между выставлением ордера и подтверждением исполнения) позиция не может
  быть закрыта по требованию (Exit/TP/SL) — это допустимое временное состояние.
- `order_placed_at: datetime` — время выставления pending-ордера (используется для TTL).
- `position_opened_at: datetime | None` — время исполнения позиции (для аудита), заполняется
  синхронно с `position_id` при исполнении ордера.
- `grid_step_at_open: float` — значение шага сетки (ATR * ATR_MULTIPLIER),
  зафиксированное в момент открытия именно этого уровня (не пересчитываемое
  позже);
- `level_index: int` — номер уровня в сетке (1 для первой позиции, далее
  по порядку);
- `signal_score_at_open: float` — score сигнала, на основании которого был
  открыт этот уровень (для аудита, не для логики).

**Важно для расчёта "пройденного расстояния" (раздел 4):** при проверке условия
"цена прошла от последней позиции расстояние не меньше шага сетки", сравнение
выполняется с `grid_step_at_open` **последней открытой позиции этой сетки**
(зафиксированным значением), а не с текущим пересчитанным ATR. Это исключает
ситуацию, когда уровень мог открыться/не открыться "задним числом" из-за того,
что ATR изменился между моментом открытия предыдущего уровня и моментом
проверки следующего.

## Ключевые правила логики

### 1. Независимость Long и Short сеток

Long Grid и Short Grid независимы на уровне механики уровней: структура грида,
шаг, лимит уровней — определяются независимо для каждой сетки. Сетки не знают
друг о друге на уровне внутренней логики расчёта уровней.

Уточнение: дисбаланс-контроль (раздел 7) модифицирует входной порог `ADD_LEVEL_THRESHOLD`
одной сетки на основе состояния объёма обеих сеток. Это не нарушение принципа
независимости механики уровней, а портфельный риск-лимит. "Независимость"
относится только к структуре и расчёту шага грида, но не к входным параметрам
решения о добавлении уровня. Общий `baseline_equity` и глобальные метрики
PnL/режимов деградации в `PortfolioManager` — это также не нарушение принципа,
так как это единственная заявленная точка пересечения на уровне портфельного риска.

### 2. Открытие первой позиции сетки

Сигнал (после маппинга) совпадает по направлению с сеткой, score не ниже порога
входа, сетка не активна (нет открытых позиций).

### 3. Динамический шаг сетки

`grid_step = ATR(14, Wilder smoothing) * ATR_MULTIPLIER`. Расчёт ATR — по
классической формуле Уайлдера: первое значение — простая средняя первых
`ATR_PERIOD` значений True Range, далее экспоненциальное сглаживание
`ATR_i = (ATR_{i-1} * (period - 1) + TR_i) / period`. Зафиксировать именно эту
формулу инициализации в коде и в тесте с известным результатом, чтобы не
было расхождения между реализацией и ожидаемым значением теста.

### 4. Добавление нового уровня в сетку

Разрешено только при одновременном выполнении всех условий:
- сетка активна;
- `abs(current_price - last_position.entry_price) >= last_position.grid_step_at_open`;
- сканер выдал новый сигнал в том же направлении (после маппинга);
- score сигнала не ниже `ADD_LEVEL_THRESHOLD` (с учётом возможной надбавки от
  дисбаланс-контроля и режима деградации — разделы 7 и 8);
- `portfolio.can_expand(direction)` (раздел 7);
- текущий режим деградации не запрещает расширение (Normal или Conservative —
  не Freeze/Exit).

Жёсткий потолок `MAX_GRID_LEVELS` на одну сетку. Усреднение по факту движения
цены без нового сигнала запрещено (см. условия выше — они все обязательны
одновременно).

### 5. Контрсетка

Активная сетка одного направления не блокирует запуск независимой сетки
противоположного направления при выполнении условий раздела 2 для этого
направления. Обе сетки работают одновременно, без взаимного влияния на уровне
логики уровней (см. уточнение в разделе 1).

### 6. Execution-проверка перед каждым открытием позиции

Используя `spread_history` и `atr_baseline_history` (см. "Внутреннее состояние"):

```
average_spread_recent = mean(spread_history[symbol])      # если окно не заполнено — проверка пропускается
current_spread = get_spread(pair)
ATR_baseline = mean(atr_baseline_history[symbol])           # если окно не заполнено — проверка пропускается
current_ATR = compute_atr(...)
```

Отказ (любое из двух достаточно):
```
current_spread > SPREAD_REJECT_MULTIPLIER * average_spread_recent
current_ATR > ATR_SPIKE_MULTIPLIER * ATR_baseline
```

Warn-зона (`SPREAD_WARN_MULTIPLIER < SPREAD_REJECT_MULTIPLIER`, аналогично по
ATR): объём умножается на `EXECUTION_WARN_VOLUME_REDUCTION`, ордер всё равно
отправляется.

Результат — `ExecutionApproval(approved: bool, volume_multiplier: float)`.

`consecutive_execution_rejections` (глобальный счётчик) инкрементируется при
`approved == False`, сбрасывается в `0` при `approved == True`.

### 7. Менеджер портфеля

#### Контроль суммарной экспозиции

```
total_exposure_lots = long_grid.total_exposure_lots() + short_grid.total_exposure_lots()
```
Сравнивается с лимитом, производным от `MAX_TOTAL_EXPOSURE` и текущего equity.
`MAX_TOTAL_EXPOSURE` (float, доля 0.0-1.0) реализуется через оценку маржи через
`ProtoOAExpectedMarginReq`: для гипотетического объёма 1 лота запрашивается оценка
маржи у брокера, затем лимит рассчитывается как `equity * MAX_TOTAL_EXPOSURE / margin_per_lot`.
Это корректно учитывает динамическое плечо, тип расчёта маржи (MAX/SUM/NET) и
валютные конвертации, которые реализованы на стороне брокера.

`can_expand(direction: Direction) -> bool` возвращает `False`, если:
- `total_exposure_lots` после гипотетического добавления стандартного шага
  объёма превысила бы лимит от `MAX_TOTAL_EXPOSURE`; **или**
- сработал жёсткий порог дисбаланса (`IMBALANCE_HARD_THRESHOLD`) именно для
  запрошенной стороны (см. ниже).

#### Дисбаланс-контроль

```
total_volume = long_grid.total_exposure_lots() + short_grid.total_exposure_lots()
imbalance = abs(long_volume - short_volume) / total_volume   # 0, если total_volume == 0
```
- Мягкий порог `IMBALANCE_SOFT_THRESHOLD`: `ADD_LEVEL_THRESHOLD` для перевешенной
  стороны повышается на `IMBALANCE_SCORE_PENALTY`.
- Жёсткий порог `IMBALANCE_HARD_THRESHOLD` (> мягкого): расширение перевешенной
  стороны полностью блокируется через `can_expand()`, до снижения дисбаланса
  ниже мягкого порога.

#### База для PnL портфеля

`baseline_equity` через `get_balance()` при первом запуске `TradingEngine`,
пересчитывается только при явном `restart()`.

```
# Guard-проверка на деление на ноль
if baseline_equity == 0:
    logger.critical("baseline_equity == 0, halting system")
    halted = True
    return

portfolio_pnl_fraction = sum(p['profit'] for p in client.get_positions(force=True)) / baseline_equity
```

**Важно:** используется `get_positions(force=True)` для гарантии актуальных данных
при расчёте PnL, так как кэш может устареть (например, позиция закрылась по SL брокера,
но кэш ещё не обновлён).

**Поведение при `restart()`:**
- `baseline_equity` — пересчитывается через `get_balance()`
- `spread_history`, `atr_baseline_history` — **сохраняются** (рыночные данные)
- `consecutive_execution_rejections` — **сбрасывается в 0**
- `halted` — устанавливается в `False`

### 8. Глобальный Take Profit, Stop Loss и режимы деградации

```
portfolio_pnl_fraction >= PORTFOLIO_TARGET           -> close_all_positions(), halted = True
portfolio_pnl_fraction <= MAX_PORTFOLIO_DRAWDOWN      -> close_all_positions(), halted = True
```

**Обработка `GridPosition` с `position_id=None` в глобальном TP/SL:** перед вызовом
`close_all_positions()` выполнить дополнительную reconciliation-проверку для всех записей
в `GridManager` с `position_id=None` (аналогично Exit-режиму). Если `position_id` успешно
разрешён — включить позицию в `close_all_positions()`. Если не разрешён — исключить из
выбора и отдельно отменить как pending через `cancel_order_by_client_id()`.

**Режимы (приоритет сверху вниз):**

1. **Exit**: `portfolio_pnl_fraction <= EXIT_MODE_DRAWDOWN_THRESHOLD` (строго
   между `0` и `MAX_PORTFOLIO_DRAWDOWN`). Новые уровни не открываются.
   `close_worst_position()`: среди всех открытых позиций обеих сеток выбирается
   позиция с минимальным `profit` через `get_positions(force=True)` для гарантии
   актуальных данных.

   **Обработка `GridPosition` с `position_id=None`:** перед выбором "худшей" позиции
   выполнить дополнительную reconciliation-проверку для всех записей в `GridManager`
   с `position_id=None`: попытаться разрешить через `pending_order_metadata`/`get_positions()`
   по алгоритму сопоставления из шага 0. Если `position_id` успешно разрешён — включить
   позицию в выбор `close_worst_position()`. Если не разрешён (позиция всё ещё pending
   у брокера или reconciliation не смог сопоставить) — исключить из выбора и отдельно
   отменить как pending через `cancel_order_by_client_id()` по `client_order_id` из
   `pending_order_metadata`. Логировать как info.

   **Fallback при всех прибыльных позициях**
   (теоретически возможно при резком развороте после серии локальных убытков,
   из-за комиссий/проскальзывания): если минимальный `profit` среди всех
   позиций положителен, всё равно закрывается позиция с минимальным `profit`
   (то есть "наименее прибыльная", не "наиболее убыточная" в строгом смысле) —
   правило не меняется, просто его трактовка корректна и в этом крайнем случае,
   так как "минимальный profit" определено всегда, независимо от знака.
2. **Freeze**: `current_ATR > FREEZE_ATR_SPIKE_MULTIPLIER * ATR_baseline` ИЛИ
   `consecutive_execution_rejections >= FREEZE_CONSECUTIVE_REJECTIONS`. Новые
   уровни не открываются. Существующие открытые позиции остаются открытыми без
   новых действий, кроме TTL-отмены pending и стандартного SL — никакого
   дополнительного управления (трейлинг и т.п.) в этой версии не реализуется.
3. **Conservative**: `current_ATR > ATR_SPIKE_MULTIPLIER * ATR_baseline` ИЛИ
   `consecutive_execution_rejections >= CONSECUTIVE_REJECTIONS_FOR_CONSERVATIVE`.
   `ADD_LEVEL_THRESHOLD` + `CONSERVATIVE_SCORE_PENALTY`; объём ×
   `CONSERVATIVE_VOLUME_MULTIPLIER` (< 1.0).
4. **Normal**: иначе.

При переходе сетки в Freeze/Exit — все её неисполненные pending-ордера (грид-
уровни, выставленные как лимитники) отменяются **выборочно по совпадению
`client_order_id` с тем, что хранится в `GridPosition`/`pending_order_metadata`
этой сетки**. Для каждого `client_order_id` из метадаты грид-уровней:
- Если orchestrator знает об этом ордере — вызвать `orchestrator.cancel_tracked_limit_order(pair)`
- Если orchestrator НЕ знает (случай упавшего `track_limit_order()`) — отменить
  напрямую через `cancel_order_by_client_id(client_order_id)` и логировать как warning.

**Допущение:** в этой версии у пары нет других источников pending-ордеров кроме
грид-уровней Trading Layer. Если это не так — необходимо изменить логику на
фильтрацию по `client_order_id` из `pending_order_metadata`.

**Поведение при отсутствии ордера в orchestrator:** если `track_limit_order()`
упал с исключением и orchestrator не знает о pending-ордере, при переходе в
Freeze/Exit Trading Layer должен проверить `get_pending_orders(force=True)` и при
наличии отменить напрямую через `cancel_order_by_client_id()`. Логировать как
warning рассинхронизацию между orchestrator и брокером.

**Определение "цикла" для TTL:** TTL считается по timestamp:
`(current_bar.timestamp.timestamp() * 1000 - order_placed_at.timestamp() * 1000) > GRID_ORDER_TTL_BARS * bar_duration`.
Это устойчиво к пропуску циклов из-за ошибок.

**Единицы измерения:** `Candle.timestamp` - это `datetime` (Python datetime объект),
не миллисекунды. Для расчёта TTL нужно конвертировать: `current_bar.timestamp.timestamp() * 1000`
получает миллисекунды. `bar_duration = state.entry_minutes * 60 * 1000` (миллисекунды).

### 9. Расчёт объёма позиции

```
risk_amount = equity * RISK_PER_TRADE
sl_distance_price = current_ATR * SL_ATR_MULTIPLIER
pip_value = orchestrator._client.get_pair_info(pair)[3]  # индекс 3 = pip_value

# Guard-проверки на деление на ноль
if baseline_equity == 0:
    logger.critical("baseline_equity == 0, halting system")
    halted = True
    return
if sl_distance_price == 0 or pip_value == 0:
    logger.warning("sl_distance_price or pip_value == 0, execution rejection")
    consecutive_execution_rejections += 1
    return  # ордер не отправляется

raw_volume = risk_amount / (sl_distance_price * pip_value)
volume = floor(raw_volume / step_volume) * step_volume
volume = clamp(volume, min_volume, max_volume)
```
`step_volume`, `min_volume`, `max_volume` — параметры, передаваемые в
`place_limit_order` (уже существующие параметры брокерского метода, не новые).
`pip_value` получается через `orchestrator._client.get_pair_info(pair)` — это
тот же механизм, который использует существующий `StrategyOrchestrator.update()`
(см. строки 72-75 в orchestrator.py), поэтому не требует отдельной проверки.

**Обработка ошибок `get_pair_info()`:** если `get_pair_info(pair)` вернул `None`
или выбросил исключение — это критическая ошибка. Цикл `on_bar_update()`
прерывается, состояние не меняется, логируется как `logger.critical()`. На
следующем цикле попытка повторяется.

Без мартингейла: `volume` каждого нового уровня рассчитывается заново по этой
формуле от текущего `equity` и текущего `current_ATR` — объём НЕ зависит от
результата (PnL) предыдущих уровней той же сетки. Тест на отсутствие
мартингейла должен проверять именно это свойство: при искусственно убыточном
предыдущем уровне объём следующего уровня рассчитывается так же, как если бы
предыдущий уровень был прибыльным при тех же equity/ATR/score.

Порядок корректировок: базовый расчёт → ограничение режимом деградации
(Conservative) → корректировка execution-проверкой (warn-зона) → финальный
объём.

## Явно не реализовывать в этой версии

- вероятностный regime engine;
- market microstructure layer на основе order flow / L2 данных;
- signal fusion engine без определённой математической модели;
- немодельная non-linear scaling позиции;
- мультисимвольная торговля с раздельными счётчиками деградации на символ
  (текущая версия — один торгуемый символ, единый глобальный счётчик).

## Конфигурация

```
ENTRY_THRESHOLD, ADD_LEVEL_THRESHOLD
ATR_PERIOD, ATR_MULTIPLIER
MAX_GRID_LEVELS
MAX_PORTFOLIO_DRAWDOWN, PORTFOLIO_TARGET, MAX_TOTAL_EXPOSURE
RISK_PER_TRADE, SL_ATR_MULTIPLIER

SPREAD_LOOKBACK_BARS, SPREAD_REJECT_MULTIPLIER, SPREAD_WARN_MULTIPLIER
ATR_BASELINE_LOOKBACK_BARS, ATR_SPIKE_MULTIPLIER
EXECUTION_WARN_VOLUME_REDUCTION

IMBALANCE_SOFT_THRESHOLD, IMBALANCE_HARD_THRESHOLD, IMBALANCE_SCORE_PENALTY

EXIT_MODE_DRAWDOWN_THRESHOLD
FREEZE_ATR_SPIKE_MULTIPLIER, FREEZE_CONSECUTIVE_REJECTIONS
CONSERVATIVE_SCORE_PENALTY, CONSERVATIVE_VOLUME_MULTIPLIER
CONSECUTIVE_REJECTIONS_FOR_CONSERVATIVE

GRID_ORDER_TTL_BARS
REFERENCE_LOT_VALUE   # временная заглушка для MAX_TOTAL_EXPOSURE, см. раздел 7
WATCHED_INSTRUMENTS   # список (symbol, timeframe)
```

Расширить существующий конфиг проекта, не создавать дублирующий файл.

## Требования к коду

- ООП, явное разделение ответственности.
- Вся логика принятия решений — чистые методы, принимающие значения (цену, ATR,
  equity, score, execution-метрики, состояние режима), не дёргающие брокера
  внутри самого решения.
- Взаимодействие с Broker/Scanner Layer — только через существующие методы и
  зафиксированные допущения.

## Тесты

Покрыть как минимум:
- открытие первой позиции при выполнении/невыполнении условий;
- каждое условие добавления уровня по отдельности, включая сравнение с
  `grid_step_at_open` зафиксированным, а не пересчитанным ATR;
- потолок MAX_GRID_LEVELS;
- независимость Long/Short сеток на уровне механики уровней (структура, шаг);
- ATR по формуле Уайлдера на наборе данных с известным результатом;
- расчёт объёма по формуле раздела 9, включая отсутствие мартингейла
  (объём не зависит от PnL предыдущего уровня при одинаковых прочих условиях);
- округление объёма до `step_volume` и ограничение `min_volume`/`max_volume`;
- guard-проверки на деление на ноль в формулах объёма и PnL;
- переходы Normal/Conservative/Freeze/Exit на границах порогов, с приоритетом
  Freeze над Conservative;
- Exit-режим: закрытие позиции с минимальным `profit` через `get_positions(force=True)`,
  включая крайний случай, когда все позиции прибыльны;
- дисбаланс-контроль: `can_expand(direction)` различается для LONG/SHORT при
  дисбалансе, мягкий и жёсткий пороги отдельно;
- `MAX_TOTAL_EXPOSURE`: `can_expand()` возвращает `False` при превышении лимита
  суммарной экспозиции, независимо от дисбаланса. **Примечание:** этот тест пишется
  на заглушке `REFERENCE_LOT_VALUE` и подлежит пересмотру после реализации
  `get_expected_margin()` в `ctrader_client.py`;
- execution-проверка: спред/ATR-spike отдельно, warn-зона снижает объём;
- холодный старт: execution-проверка не блокирует при незаполненном окне истории;
- `consecutive_execution_rejections` инкремент/сброс;
- глобальный TP/SL закрывает обе сетки, выставляет `halted`;
- запрет открытия после `halted` до `restart()`, `baseline_equity` не меняется
  до `restart()`;
- маппинг BUY/SELL → LONG/SHORT;
- TTL pending-ордера: отмена после `GRID_ORDER_TTL_BARS` без исполнения;
- отмена pending-ордеров при переходе в Freeze/Exit (выборочно по `client_order_id`);
- проверка `get_positions(force=True)` после закрытия и обработка случая,
  когда позиция не исчезла (логирование критической ошибки, не бесконечный retry);
- reconciliation (шаг 0): удаление из памяти позиций, которых нет у брокера;
- reconciliation: восстановление `GridPosition` после исполнения pending между циклами
  (заполнение `position_id` и `position_opened_at` из `pending_order_metadata`);
- reconciliation: warning при рассинхронизации без соответствующего `client_order_id`.

## Что нужно на выходе

1. Код Trading Layer, интегрированный с Broker Layer и Scanner Layer.
2. Тесты, покрывающие пункты выше.
3. Краткое резюме (в чате, не в коде):
   - подтверждение или коррекция каждого ДОПУЩЕНИЯ;
   - результат проверки по каждому пункту, помеченному ТРЕБУЕТ ПРОВЕРКИ —
     с конкретным значением/формулой, найденной в реальном коде, а не повторное
     допущение вместо проверки.
