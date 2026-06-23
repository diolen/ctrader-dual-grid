import asyncio
import logging
import time
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional

import ctrader_open_api.messages.OpenApiModelMessages_pb2 as model_proto

from app.connection.ctrader_client import CTraderClient
from app.connection.warmup_cache import warmup_candle_cache
from app.connection.market_cache import MarketCache
from app.strategy.orchestrator import StrategyOrchestrator
from app.strategy.backtest import run_backtest_v3
from app.trading.executor import TradeExecutor
from app.core.recommender import AnalysisResult
from app.trading.trading_engine import TradingEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s")
# Рутинные переходы pending/fill/close — DEBUG; не засоряют консоль при root DEBUG
logging.getLogger("app.strategy.trade_guard").setLevel(logging.INFO)

TIMEFRAME_MAP = {
    "M1": model_proto.M1,
    "M2": model_proto.M2,
    "M3": model_proto.M3,
    "M4": model_proto.M4,
    "M5": model_proto.M5,
    "M10": model_proto.M10,
    "M15": model_proto.M15,
    "M30": model_proto.M30,
    "H1": model_proto.H1,
    "H4": model_proto.H4,
    "D1": model_proto.D1,
}

TIMEFRAME_MINUTES = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5,
    "M10": 10, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}

RECONNECT_DELAYS = [5, 15, 30, 60, 120]
POST_WARMUP_COOLDOWN_SEC = 3.0
_last_api_metrics_log: float = 0.0


def _pair_poll_delay_sec(num_pairs: int) -> float:
    """Auto-scales poll delay for large watchlists (cTrader historical 5 req/s)."""
    from app.config.settings import config

    if config.PAIR_POLL_DELAY_SEC > 0:
        return config.PAIR_POLL_DELAY_SEC
    if num_pairs <= 12:
        return 0.4
    if num_pairs <= 16:
        return 0.6
    return 0.8




def _maybe_log_api_metrics(
    client: CTraderClient,
    *,
    force: bool = False,
) -> None:
    global _last_api_metrics_log
    from app.config.settings import config

    if config.API_METRICS_LOG_INTERVAL_SEC <= 0:
        return
    now = time.monotonic()
    if not force and (now - _last_api_metrics_log) < config.API_METRICS_LOG_INTERVAL_SEC:
        return
    _last_api_metrics_log = now
    # Screener loop sets root logger to WARNING — temporarily allow INFO for metrics.
    root = logging.getLogger()
    prev_level = root.level
    if prev_level > logging.INFO:
        root.setLevel(logging.INFO)
    try:
        client.api_metrics.log_summary()
    finally:
        if prev_level > logging.INFO:
            root.setLevel(prev_level)


def _sync_warmup_cache(pair: str, candles: list) -> None:
    from app.config.settings import config

    if config.WARMUP_CACHE_ENABLED and candles:
        warmup_candle_cache.sync_candles(pair, candles)


async def _fetch_warmup_candles(
    client: CTraderClient,
    pair: str,
    symbol_id: int,
    entry_tf: str,
    entry_period: int,
    entry_minutes: int,
    warmup_bars: int,
) -> list:
    from app.config.settings import config

    now = int(time.time())
    expected_ms = _expected_closed_bar_open_ms(now, entry_minutes)
    bar_ms = entry_minutes * 60 * 1000

    if config.WARMUP_CACHE_ENABLED:
        cached = warmup_candle_cache.try_get(
            pair,
            symbol_id=symbol_id,
            entry_tf=entry_tf,
            entry_minutes=entry_minutes,
            expected_closed_bar_ms=expected_ms,
            min_bars=warmup_bars,
        )
        if cached is not None:
            return cached

    logging.info(f"⏳ [{pair}] Загружаю {warmup_bars} свечей {entry_tf} для прогрева...")
    candles = await client.get_trendbars_chunked(
        symbol_id,
        entry_period,
        (now - warmup_bars * entry_minutes * 60) * 1000,
        (now - 60) * 1000,
        bar_ms=bar_ms,
        pair=pair,
        timeframe=entry_tf,
        verbose=True,
    )

    if config.WARMUP_CACHE_ENABLED and candles:
        warmup_candle_cache.store(
            pair,
            candles,
            symbol_id=symbol_id,
            entry_tf=entry_tf,
            entry_minutes=entry_minutes,
            min_bars=warmup_bars,
        )
    return candles


def _apply_strategy_pair_config(strategy, client: CTraderClient, pair: str) -> None:
    """Per-pair конфиг и pip_value для screener (как в StrategyOrchestrator.update)."""
    from app.config.settings import config

    strategy.pair_config = config.get_pair_config(pair)
    info = client.get_pair_info(pair)
    if info and len(info) > 3:
        strategy.pip_value = float(info[3])


def _spread_price(
    market_cache: Optional[MarketCache],
    client: CTraderClient,
    pair: str,
) -> Optional[float]:
    """Спред bid-ask в единицах цены для фильтра стратегии."""
    if market_cache is None:
        return None
    spread_pips = market_cache.get_spread(pair)
    if spread_pips is None:
        return None
    info = client.get_pair_info(pair)
    pip_value = float(info[3]) if info and len(info) > 3 else 0.0001
    return spread_pips * pip_value


def _debug_stats_interval(entry_minutes: int) -> int:
    from app.config.settings import config

    if config.STRATEGY_DEBUG_EVERY_N > 0:
        return config.STRATEGY_DEBUG_EVERY_N
    return 5 if entry_minutes == 1 else 25


def _parse_backtest_bars() -> int:
    from app.config.settings import config

    bars = config.BACKTEST_BARS
    for arg in sys.argv[1:]:
        if arg.startswith("--backtest-bars="):
            try:
                bars = int(arg.split("=", 1)[1])
            except ValueError:
                pass
    return bars


def _candle_ts_ms(candle) -> int:
    ts = candle.timestamp
    if isinstance(ts, datetime):
        return int(ts.timestamp() * 1000)
    if ts > 10_000_000_000:
        return int(ts)
    return int(ts) * 1000


def _expected_closed_bar_open_ms(now: int, bar_minutes: int) -> int:
    """Open time (ms) последней полностью закрытой свечи на момент now."""
    bar_sec = bar_minutes * 60
    return ((now // bar_sec) * bar_sec - bar_sec) * 1000


def _filter_bars_up_to(bars: list, max_open_ms: int) -> list:
    """Отбрасывает свечи из будущего (API иногда отдаёт формирующийся бар раньше)."""
    return [c for c in bars if _candle_ts_ms(c) <= max_open_ms]


async def _fetch_latest_bars(
    client: CTraderClient,
    symbol_id: int,
    period: int,
    bar_minutes: int,
    buffer: list,
    pair: str,
    timeframe: str,
    poll_now: Optional[int] = None,
    retries: int = 4,
    retry_delay: float = 2.0,
) -> tuple[list, str]:
    """
    Запрос последних баров. Возвращает (bars, status):
    ok — есть новые бары; synced — буфер уже актуален; miss — бар не получен.
    """
    bar_sec = bar_minutes * 60
    bar_ms = bar_sec * 1000

    for attempt in range(retries):
        now = int(time.time()) if attempt > 0 or poll_now is None else poll_now
        expected_ms = _expected_closed_bar_open_ms(now, bar_minutes)

        if buffer and _candle_ts_ms(buffer[-1]) >= expected_ms:
            return [], "synced"

        if buffer:
            from_ms = _candle_ts_ms(buffer[-1]) - bar_ms
        else:
            from_ms = (now - 10 * bar_sec) * 1000
        to_ms = expected_ms + bar_ms

        bars = await client.get_trendbars_chunked(
            symbol_id,
            period,
            from_ms,
            to_ms,
            bar_ms=bar_ms,
            pair=pair,
            timeframe=timeframe,
        )
        if bars:
            valid = _filter_bars_up_to(bars, expected_ms)
            last_ms = _candle_ts_ms(buffer[-1]) if buffer else 0
            if any(_candle_ts_ms(c) > last_ms for c in valid):
                return valid, "ok"
            if buffer and _candle_ts_ms(buffer[-1]) >= expected_ms:
                return [], "synced"

        if attempt < retries - 1:
            await asyncio.sleep(retry_delay)

    return [], "miss"


def _merge_new_candles(buffer: list, incoming: list, max_len: int = 2000) -> int:
    """Добавляет только новые свечи по timestamp. Возвращает число добавленных."""
    existing_ts = {c.timestamp for c in buffer}
    added = 0
    for c in incoming:
        if c.timestamp not in existing_ts:
            buffer.append(c)
            existing_ts.add(c.timestamp)
            added += 1
    if len(buffer) > max_len:
        buffer[:] = buffer[-max_len:]
    return added


def _seconds_until_next_bar(entry_minutes: int, offset_sec: float = 4.0) -> float:
    """
    Секунды до offset_sec после закрытия следующей свечи.
    Выравнивает цикл по границе M1/M5, чтобы не пропускать бары из-за drift sleep(60).
    """
    bar_sec = entry_minutes * 60
    elapsed = time.time() % bar_sec
    return bar_sec - elapsed + offset_sec


@dataclass
class _PairRuntime:
    pair: str
    symbol_id: int
    candles: list
    entry_tf: str
    entry_period: int
    entry_minutes: int
    debug_counter: int = 0




async def _warmup_pair(
    client: CTraderClient,
    pair: str,
    orchestrator: StrategyOrchestrator,
) -> Optional[_PairRuntime]:
    """Прогрев M5 + cold_start FSM."""
    from app.config.settings import config
    from app.strategy.base import MarketData

    info = client.get_pair_info(pair)
    if not info:
        logging.error(f"❌ Пара {pair} не инициализирована, пропускаем")
        return None

    symbol_id = info[0]
    pair_cfg = config.get_pair_config(pair)
    entry_tf = pair_cfg.entry_timeframe
    entry_period = TIMEFRAME_MAP.get(entry_tf, model_proto.M5)
    entry_minutes = TIMEFRAME_MINUTES.get(entry_tf, 5)
    logging.info(f"📊 [{pair}] Breakout Retest: {pair_cfg}")

    warmup_bars = config.warmup_bars_for_pair(pair)
    candles = await _fetch_warmup_candles(
        client, pair, symbol_id, entry_tf, entry_period, entry_minutes, warmup_bars,
    )

    for i in range(len(candles)):
        market_data = MarketData(
            pair=pair,
            candles=candles[: i + 1],
            is_warmup=True,
        )
        await orchestrator.update(market_data)

    orchestrator.cold_start_pair(pair, candles)
    logging.info(f"🚀 [{pair}] Стратегия прогрета + cold_start.")
    return _PairRuntime(
        pair=pair,
        symbol_id=symbol_id,
        candles=candles,
        entry_tf=entry_tf,
        entry_period=entry_period,
        entry_minutes=entry_minutes,
    )


async def _handle_signal(
    signal,
    pair: str,
    entry_tf: str,
    client: CTraderClient,
    orchestrator: StrategyOrchestrator,
    executor: TradeExecutor,
    market_cache: MarketCache,
    *,
    reason: str = "",
) -> None:
    from app.config.settings import config

    pair_info = client.get_pair_info(pair)
    log_digits = pair_info[2] if pair_info else 5
    pair_cfg = config.get_pair_config(pair)
    max_spread = pair_cfg.max_spread_pips

    spread = market_cache.get_spread(pair)
    if spread is not None and spread > max_spread:
        logging.warning(
            f"⚠️ [{pair}] Spread {spread:.1f}p > {max_spread:.1f}p — сигнал отклонён"
        )
        await orchestrator.release_pair_blocks(pair)
        return

    signal = replace(signal, spread_at_entry=spread)
    prefix = f"🔔 [{pair}]"
    if reason:
        prefix += f" ({reason})"
    line = signal.format_log_line(log_digits)
    if spread is None:
        line += f" | spread=N/A (max {max_spread:.1f}p)"
    else:
        line += f" | max {max_spread:.1f}p"
    logging.info(f"{prefix}: {line}")

    if config.TRADING_MODE == "AUTO":
        logging.info(f"🤖 [{pair}] Режим AUTO — отправляю ордер...")
        order_id = None
        try:
            order_id = await executor.execute(signal, pair=pair)
        except Exception as e:
            logging.error(f"❌ [{pair}] Ошибка исполнения ордера: {e}", exc_info=True)
        if order_id:
            await orchestrator.track_limit_order(
                pair, order_id, signal.strategy_type or "",
            )
            await orchestrator.mark_order_pending(order_id, pair)
        else:
            await orchestrator.release_pair_blocks(pair)
    else:
        logging.info(f"✋ [{pair}] Режим MANUAL — ордер не отправлен")


async def _run_strategy_tick(
    state: _PairRuntime,
    orchestrator: StrategyOrchestrator,
    client: CTraderClient,
    executor: TradeExecutor,
    market_cache: MarketCache,
    trading_engine: Optional[TradingEngine] = None,
    *,
    reason: str = "",
) -> None:
    """Один проход стратегии по текущему буферу свечей."""
    from app.config.settings import config
    
    # Use Trading Engine if DUAL_GRID_V8 strategy
    if config.STRATEGY_TYPE == "DUAL_GRID_V8" and trading_engine:
        await trading_engine.on_bar_update(state, orchestrator, client, market_cache)
        return
    
    # Original strategy logic
    from app.strategy.base import MarketData

    market_data = MarketData(
        pair=state.pair,
        candles=state.candles,
        spread=_spread_price(market_cache, client, state.pair),
    )
    signal = await orchestrator.update(market_data)
    if signal:
        await _handle_signal(
            signal,
            state.pair,
            state.entry_tf,
            client,
            orchestrator,
            executor,
            market_cache,
            reason=reason,
        )


async def _poll_pair(
    client: CTraderClient,
    state: _PairRuntime,
    orchestrator: StrategyOrchestrator,
    executor: TradeExecutor,
    market_cache: MarketCache,
    poll_now: int,
    trading_engine: Optional[TradingEngine] = None,
) -> None:
    """Один тик опроса M5 → стратегия → сигнал."""
    pair = state.pair
    entry_tf = state.entry_tf
    entry_period = state.entry_period
    entry_minutes = state.entry_minutes
    strategy = orchestrator.get_active_strategy()
    force_tick = (
        hasattr(strategy, "needs_update_without_new_bar")
        and strategy.needs_update_without_new_bar(pair)
    )

    new_candles, bar_status = await _fetch_latest_bars(
        client,
        state.symbol_id,
        entry_period,
        entry_minutes,
        state.candles,
        pair,
        entry_tf,
        poll_now=poll_now,
    )

    if bar_status == "miss":
        logging.warning(f"📊 [{pair}] {entry_tf} — бар не готов после повторов")
        return

    bars_added = 0
    if bar_status == "ok":
        bars_added = _merge_new_candles(state.candles, new_candles)

    if bars_added == 0 and not force_tick:
        logging.debug(f"📊 [{pair}] {entry_tf} — без новых баров")
        return

    if bars_added > 0:
        suffix = " (догон)" if bars_added > 1 else ""
        logging.info(f"📊 [{pair}] {entry_tf} +{bars_added}{suffix}")
        _sync_warmup_cache(pair, state.candles)
    elif force_tick:
        status = (
            strategy.pair_status_line(pair, bars_count=len(state.candles))
            if hasattr(strategy, "pair_status_line")
            else "tick"
        )
        logging.info(f"📊 [{pair}] live-tick без нового бара | {status}")

    await _run_strategy_tick(
        state,
        orchestrator,
        client,
        executor,
        market_cache,
        trading_engine,
        reason="новый M5" if bars_added else "live-tick",
    )

    state.debug_counter += 1
    debug_interval = _debug_stats_interval(entry_minutes)
    if debug_interval > 0 and state.debug_counter % debug_interval == 0:
        if hasattr(strategy, "print_debug"):
            logging.info(f"🔍 [{pair}] Debug статистика (итерация {state.debug_counter}):")
            strategy.print_debug()


def _log_pair_fsm(states: list[_PairRuntime], orchestrator: StrategyOrchestrator) -> None:
    strategy = orchestrator.get_active_strategy()
    if not hasattr(strategy, "pair_status_line"):
        return
    parts = [
        f"{s.pair}={strategy.pair_status_line(s.pair, bars_count=len(s.candles))}"
        for s in states
    ]
    logging.info(f"📋 FSM: {' | '.join(parts)}")


async def _poll_all_pairs(
    client: CTraderClient,
    states: list[_PairRuntime],
    orchestrator: StrategyOrchestrator,
    executor: TradeExecutor,
    market_cache: MarketCache,
    poll_now: int,
    trading_engine: Optional[TradingEngine] = None,
    *,
    cycle_label: str = "",
) -> None:
    if cycle_label:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        logging.info(f"⏱️ [{ts}] {cycle_label}")
    for idx, state in enumerate(states):
        if idx > 0:
            delay = _pair_poll_delay_sec(len(states))
            if delay > 0:
                await asyncio.sleep(delay)
        await _poll_pair(
            client,
            state,
            orchestrator,
            executor,
            market_cache,
            poll_now,
            trading_engine,
        )
    _log_pair_fsm(states, orchestrator)
    _maybe_log_api_metrics(client)


async def _run_bar_coordinator(
    client: CTraderClient,
    pairs: list[str],
    orchestrator: StrategyOrchestrator,
    executor: TradeExecutor,
    market_cache: MarketCache,
) -> None:
    """Единый цикл опроса свечей: одно пробуждение на бар, пары последовательно."""
    from app.config.settings import config
    
    # Initialize TradingEngine if DUAL_GRID_V8 strategy
    trading_engine: Optional[TradingEngine] = None
    if config.STRATEGY_TYPE == "DUAL_GRID_V8":
        trading_engine = TradingEngine()
        logging.info("🚀 TradingEngine initialized for DUAL_GRID_V8 strategy")
    
    states: list[_PairRuntime] = []
    for pair in pairs:
        state = await _warmup_pair(client, pair, orchestrator)
        if state:
            states.append(state)

    if not states:
        logging.error("❌ Нет пар для торговли после прогрева")
        return

    poll_minutes = min(s.entry_minutes for s in states)
    entry_tfs = ", ".join(f"{s.pair}:{s.entry_tf}" for s in states)
    logging.info(
        f"⏱️ Координатор опроса: {entry_tfs} | граница {poll_minutes}m +4с"
    )
    if len(states) > 1 and POST_WARMUP_COOLDOWN_SEC > 0:
        logging.info(
            f"⏳ Пауза {POST_WARMUP_COOLDOWN_SEC:.0f}s после прогрева "
            f"(снижение rate limit API)..."
        )
        await asyncio.sleep(POST_WARMUP_COOLDOWN_SEC)
        logging.info("✅ Пауза после прогрева завершена")

    _log_pair_fsm(states, orchestrator)
    _maybe_log_api_metrics(client, force=True)

    poll_now = int(time.time())
    await _poll_all_pairs(
        client,
        states,
        orchestrator,
        executor,
        market_cache,
        poll_now,
        trading_engine,
        cycle_label="Опрос после прогрева",
    )

    while True:
        wait_sec = _seconds_until_next_bar(poll_minutes)
        next_poll_utc = datetime.now(timezone.utc) + timedelta(seconds=wait_sec)
        logging.info(
            f"⏳ Ожидание M{poll_minutes} (+4с): {wait_sec:.0f}s "
            f"(следующий опрос ~{next_poll_utc.strftime('%H:%M:%S')} UTC)"
        )
        try:
            await asyncio.sleep(wait_sec)
        except asyncio.CancelledError:
            raise
        poll_now = int(time.time())
        await _poll_all_pairs(
            client,
            states,
            orchestrator,
            executor,
            market_cache,
            poll_now,
            trading_engine,
            cycle_label="Опрос M5",
        )


def _create_multi_setup_screener(pairs: list[str]) -> "MultiSetupScreener":
    from app.config.settings import config
    from app.scanner.screener_runtime import MultiSetupScreener

    pair_configs = {pair: config.get_pair_config(pair) for pair in pairs}
    return MultiSetupScreener(pair_configs=pair_configs)


def _apply_screener_pip_values(
    screener: "MultiSetupScreener",
    client: CTraderClient,
    pair: str,
) -> None:
    info = client.get_pair_info(pair)
    if info and len(info) > 3:
        screener.set_pip_value(pair, float(info[3]))




async def _poll_pair_multi_screener(
    client: CTraderClient,
    state: _PairRuntime,
    poll_now: int,
) -> bool:
    """Fetch new bars for one pair. Returns True if bars were added."""
    pair = state.pair
    entry_tf = state.entry_tf
    entry_period = state.entry_period
    entry_minutes = state.entry_minutes

    new_candles, bar_status = await _fetch_latest_bars(
        client,
        state.symbol_id,
        entry_period,
        entry_minutes,
        state.candles,
        pair,
        entry_tf,
        poll_now=poll_now,
    )

    if bar_status == "miss":
        logging.warning(f"📊 [{pair}] {entry_tf} — бар не готов после повторов")
        return False

    bars_added = 0
    if bar_status == "ok":
        bars_added = _merge_new_candles(state.candles, new_candles)

    if bars_added == 0:
        logging.debug(f"📊 [{pair}] {entry_tf} — без новых баров")
        return False

    suffix = " (догон)" if bars_added > 1 else ""
    logging.info(f"📊 [{pair}] {entry_tf} +{bars_added}{suffix}")
    _sync_warmup_cache(pair, state.candles)
    return True


async def _poll_all_pairs_multi_screener(
    client: CTraderClient,
    states: list[_PairRuntime],
    screener: "MultiSetupScreener",
    poll_now: int,
    *,
    cycle_label: str = "",
) -> None:
    if cycle_label:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        logging.info(f"⏱️ [{ts}] {cycle_label}")

    any_new = False
    for idx, state in enumerate(states):
        if idx > 0:
            delay = _pair_poll_delay_sec(len(states))
            if delay > 0:
                await asyncio.sleep(delay)
        _apply_screener_pip_values(screener, client, state.pair)
        if await _poll_pair_multi_screener(client, state, poll_now):
            any_new = True

    _maybe_log_api_metrics(client)


async def _warmup_pair_multi_screener(
    client: CTraderClient,
    pair: str,
    screener: "MultiSetupScreener",
) -> Optional[_PairRuntime]:
    from app.config.settings import config

    info = client.get_pair_info(pair)
    if not info:
        logging.error(f"❌ Пара {pair} не инициализирована, пропускаем")
        return None

    symbol_id = info[0]
    _apply_screener_pip_values(screener, client, pair)
    pair_cfg = config.get_pair_config(pair)
    entry_tf = pair_cfg.entry_timeframe
    entry_period = TIMEFRAME_MAP.get(entry_tf, model_proto.M5)
    entry_minutes = TIMEFRAME_MINUTES.get(entry_tf, 5)
    logging.info(f"📊 [{pair}] Multi-setup screener: {pair_cfg}")

    warmup_bars = config.warmup_bars_for_pair(pair)
    candles = await _fetch_warmup_candles(
        client, pair, symbol_id, entry_tf, entry_period, entry_minutes, warmup_bars,
    )

    screener.warmup_pair(pair, candles, entry_tf)
    logging.info(f"🚀 [{pair}] Multi-setup screener прогрет.")

    return _PairRuntime(
        pair=pair,
        symbol_id=symbol_id,
        candles=candles,
        entry_tf=entry_tf,
        entry_period=entry_period,
        entry_minutes=entry_minutes,
    )


async def _run_bar_coordinator_multi_screener(
    client: CTraderClient,
    pairs: list[str],
    screener: "MultiSetupScreener",
) -> None:
    states: list[_PairRuntime] = []
    for pair in pairs:
        state = await _warmup_pair_multi_screener(client, pair, screener)
        if state:
            states.append(state)

    if not states:
        logging.error("❌ Нет пар для скрининга после прогрева")
        return

    poll_minutes = min(s.entry_minutes for s in states)
    entry_tfs = ", ".join(f"{s.pair}:{s.entry_tf}" for s in states)
    logging.info(
        f"⏱️ Multi-setup координатор: {entry_tfs} | граница {poll_minutes}m +4с"
    )
    _maybe_log_api_metrics(client, force=True)

    poll_now = int(time.time())
    await _poll_all_pairs_multi_screener(
        client, states, screener, poll_now,
        cycle_label="Опрос после прогрева",
    )

    while True:
        wait_sec = _seconds_until_next_bar(poll_minutes)
        try:
            await asyncio.sleep(wait_sec)
        except asyncio.CancelledError:
            raise
        poll_now = int(time.time())
        await _poll_all_pairs_multi_screener(
            client, states, screener, poll_now,
            cycle_label="Опрос M5",
        )


async def _run_screener_tick(
    state: _PairRuntime,
    strategy,
    client: CTraderClient,
) -> Optional[AnalysisResult]:
    """Один проход стратегии в SCREENER_ONLY режиме."""
    from app.strategy.base import MarketData

    _apply_strategy_pair_config(strategy, client, state.pair)
    market_data = MarketData(
        pair=state.pair,
        candles=state.candles,
        spread=None,
    )

    await strategy.update(market_data)
    return strategy.get_analysis_result(state.pair, state.candles)


async def _poll_pair_screener(
    client: CTraderClient,
    state: _PairRuntime,
    strategy,
    poll_now: int,
) -> None:
    """Один тик опроса M5 → стратегия (SCREENER_ONLY режим)."""
    pair = state.pair
    entry_tf = state.entry_tf
    entry_period = state.entry_period
    entry_minutes = state.entry_minutes

    new_candles, bar_status = await _fetch_latest_bars(
        client,
        state.symbol_id,
        entry_period,
        entry_minutes,
        state.candles,
        pair,
        entry_tf,
        poll_now=poll_now,
    )

    if bar_status == "miss":
        logging.warning(f"📊 [{pair}] {entry_tf} — бар не готов после повторов")
        return

    bars_added = 0
    if bar_status == "ok":
        bars_added = _merge_new_candles(state.candles, new_candles)

    if bars_added == 0:
        logging.debug(f"📊 [{pair}] {entry_tf} — без новых баров")
        return

    if bars_added > 0:
        suffix = " (догон)" if bars_added > 1 else ""
        logging.info(f"📊 [{pair}] {entry_tf} +{bars_added}{suffix}")
        _sync_warmup_cache(pair, state.candles)

    await _run_screener_tick(state, strategy, client)


async def _poll_all_pairs_screener(
    client: CTraderClient,
    states: list[_PairRuntime],
    strategy,
    poll_now: int,
    *,
    cycle_label: str = "",
) -> None:
    if cycle_label:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        logging.info(f"⏱️ [{ts}] {cycle_label}")
    for idx, state in enumerate(states):
        if idx > 0:
            delay = _pair_poll_delay_sec(len(states))
            if delay > 0:
                await asyncio.sleep(delay)
        await _poll_pair_screener(
            client,
            state,
            strategy,
            poll_now,
        )
    _maybe_log_api_metrics(client)


async def _run_bar_coordinator_screener(
    client: CTraderClient,
    pairs: list[str],
    strategy,
) -> None:
    """Координатор опроса для SCREENER_ONLY режима."""
    states: list[_PairRuntime] = []
    for pair in pairs:
        state = await _warmup_pair_screener(client, pair, strategy)
        if state:
            states.append(state)

    if not states:
        logging.error("❌ Нет пар для скрининга после прогрева")
        return

    poll_minutes = min(s.entry_minutes for s in states)
    entry_tfs = ", ".join(f"{s.pair}:{s.entry_tf}" for s in states)
    logging.info(
        f"⏱️ Screener координатор: {entry_tfs} | граница {poll_minutes}m +4с"
    )
    _maybe_log_api_metrics(client, force=True)

    poll_now = int(time.time())
    await _poll_all_pairs_screener(
        client,
        states,
        strategy,
        poll_now,
        cycle_label="Опрос после прогрева",
    )

    while True:
        wait_sec = _seconds_until_next_bar(poll_minutes)
        try:
            await asyncio.sleep(wait_sec)
        except asyncio.CancelledError:
            raise
        poll_now = int(time.time())
        await _poll_all_pairs_screener(
            client,
            states,
            strategy,
            poll_now,
            cycle_label="Опрос M5",
        )


async def _warmup_pair_screener(
    client: CTraderClient,
    pair: str,
    strategy,
) -> Optional[_PairRuntime]:
    """Прогрев M5 + cold_start FSM для SCREENER_ONLY режима."""
    from app.config.settings import config
    from app.strategy.base import MarketData

    info = client.get_pair_info(pair)
    if not info:
        logging.error(f"❌ Пара {pair} не инициализирована, пропускаем")
        return None

    symbol_id = info[0]
    _apply_strategy_pair_config(strategy, client, pair)
    pair_cfg = config.get_pair_config(pair)
    entry_tf = pair_cfg.entry_timeframe
    entry_period = TIMEFRAME_MAP.get(entry_tf, model_proto.M5)
    entry_minutes = TIMEFRAME_MINUTES.get(entry_tf, 5)
    logging.info(f"📊 [{pair}] Screener: {pair_cfg}")

    warmup_bars = config.warmup_bars_for_pair(pair)
    candles = await _fetch_warmup_candles(
        client, pair, symbol_id, entry_tf, entry_period, entry_minutes, warmup_bars,
    )

    for i in range(len(candles)):
        market_data = MarketData(
            pair=pair,
            candles=candles[: i + 1],
            is_warmup=True,
        )
        await strategy.update(market_data)

    strategy.cold_start(candles, pair)
    logging.info(f"🚀 [{pair}] Скринер прогрет + cold_start.")
    return _PairRuntime(
        pair=pair,
        symbol_id=symbol_id,
        candles=candles,
        entry_tf=entry_tf,
        entry_period=entry_period,
        entry_minutes=entry_minutes,
    )


async def _run_live(config) -> None:
    client = CTraderClient()
    await client.connect()
    await client.authorize()
    await client.init_symbols()

    # SCREENER_ONLY режим
    if config.SCREENER_ONLY:
        logging.info("🎯 Режим SCREENER_ONLY: только анализ, без торговли")

        if config.USE_MULTI_SCANNER:
            logging.info(
                "🔍 Multi-setup scanner: Breakout + Pullback + "
                "LiquiditySweep + PriceAction"
            )
            screener = _create_multi_setup_screener(config.PAIRS)
            await _run_bar_coordinator_multi_screener(
                client, config.PAIRS, screener,
            )
            return

        logging.info("📈 Legacy screener: Breakout Retest v3 only")
        from app.strategy.trade_guard import TradeGuard
        from app.strategy.breakout_retest_v3 import BreakoutRetestScalpingV3Strategy

        trade_guard = TradeGuard()
        strategy = BreakoutRetestScalpingV3Strategy(trade_guard=trade_guard)
        pair_configs = {pair: config.get_pair_config(pair) for pair in config.PAIRS}
        strategy.pair_configs = pair_configs

        await _run_bar_coordinator_screener(
            client, config.PAIRS, strategy,
        )
        return

    # Обычный торговый режим
    market_cache = MarketCache(max_age_seconds=5.0)
    logging.info("📊 MarketCache инициализирован")

    for pair in config.PAIRS:
        info = client.get_pair_info(pair)
        if info:
            market_cache.set_pip_value(pair, info[3])

    await client.subscribe_quotes(market_cache)
    await asyncio.sleep(1.0)
    cached = market_cache.get_all_pairs()
    if cached:
        logging.info(f"📊 MarketCache: котировки для {', '.join(cached)}")
    else:
        logging.warning(
            "⚠️ MarketCache пуст после подписки — spread filter может не сработать до первого SpotEvent"
        )

    executor = TradeExecutor(client, market_cache=market_cache)

    pair_configs = {pair: config.get_pair_config(pair) for pair in config.PAIRS}
    orchestrator = StrategyOrchestrator(
        client=client,
        market_cache=market_cache,
        pair_configs=pair_configs,
    )
    logging.info(f"🎯 Стратегия: {orchestrator.get_strategy_type()}")
    logging.info(f"⏰ Торговое окно: {config.TRADE_WINDOW_START} - {config.TRADE_WINDOW_END} UTC")

    await orchestrator.run_recovery()

    async def pending_order_cleanup_loop():
        from app.config.settings import config as app_config

        interval = app_config.PENDING_ORDER_CLEANUP_INTERVAL_SECONDS
        try:
            await asyncio.sleep(interval)
            while True:
                try:
                    n = await orchestrator.run_pending_order_cleanup()
                    if n:
                        logging.info(f"🧹 Отменено зависших pending-ордеров: {n}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.error(
                        f"❌ Ошибка cleanup pending-ордеров: {e}",
                        exc_info=True,
                    )
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logging.debug("🧹 Cleanup loop остановлен")

    cleanup_task = asyncio.create_task(pending_order_cleanup_loop())
    try:
        await _run_bar_coordinator(
            client, config.PAIRS, orchestrator, executor, market_cache,
        )
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


async def _run_live_loop(config) -> None:
    attempt = 0
    while True:
        try:
            await _run_live(config)
        except asyncio.CancelledError:
            logging.info("🛑 Остановка по запросу пользователя.")
            break
        except Exception as e:
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            logging.error(f"❌ Соединение потеряно: {e}")
            logging.info(f"🔄 Переподключение через {delay} сек... (попытка {attempt})")
            await asyncio.sleep(delay)
        else:
            attempt = 0


async def main():
    from app.config.settings import config

    backtest_mode = "--backtest" in sys.argv
    local_backtest_mode = "--local-backtest" in sys.argv
    save_data_mode = "--save-data" in sys.argv
    optimize_mode = "--optimize" in sys.argv

    if optimize_mode:
        # Optimize parameters for a pair
        import argparse
        parser = argparse.ArgumentParser(description="Optimize backtest parameters for a pair")
        parser.add_argument("--csv", required=True, help="Path to CSV file with candle data")
        parser.add_argument("--pair", required=True, help="Trading pair (e.g., EURUSD)")
        parser.add_argument("--digits", type=int, required=True, help="Number of decimal places")
        parser.add_argument("--pip-value", type=float, required=True, help="Pip value")
        parser.add_argument("--timeframe", default="M5", help="Timeframe (default: M5)")
        parser.add_argument("--trail-pips", type=float, default=5.0, help="Trailing stop in pips")
        parser.add_argument("--max-iterations", type=int, default=50, help="Max parameter combinations to test")
        
        args = parser.parse_args(sys.argv[2:])  # Skip script name and --optimize
        
        from app.backtest.parameter_optimizer import optimize_pair_parameters
        
        logging.info(
            f"🔧 Optimizing parameters | pair: {args.pair} | CSV: {args.csv} | "
            f"max iterations: {args.max_iterations}"
        )
        
        await optimize_pair_parameters(
            pair=args.pair,
            csv_file=args.csv,
            digits=args.digits,
            pip_value=args.pip_value,
            timeframe=args.timeframe,
            trail_pips=args.trail_pips,
            max_iterations=args.max_iterations,
        )
        return

    if save_data_mode:
        # Save live data from API to CSV files
        import argparse
        parser = argparse.ArgumentParser(description="Save live data from API to CSV")
        parser.add_argument("--pair", required=True, help="Trading pair (e.g., EURUSD)")
        parser.add_argument("--timeframe", default="M5", help="Timeframe (default: M5)")
        parser.add_argument("--bars", type=int, default=1000, help="Number of bars to fetch (default: 1000)")
        parser.add_argument("--output-dir", default="data", help="Output directory (default: data)")
        
        args = parser.parse_args(sys.argv[2:])  # Skip script name and --save-data
        
        client = CTraderClient()
        await client.connect()
        await client.authorize()
        await client.init_symbols()
        
        info = client.get_pair_info(args.pair)
        if not info:
            logging.error(f"❌ Пара {args.pair} не найдена")
            return
        
        symbol_id, _, digits, pip_value, _, _ = info
        
        from app.backtest.live_data_saver import save_live_data_from_api
        
        logging.info(
            f"💾 Saving live data | pair: {args.pair} | timeframe: {args.timeframe} | "
            f"bars: {args.bars} | output: {args.output_dir}"
        )
        
        await save_live_data_from_api(
            client=client,
            pair=args.pair,
            symbol_id=symbol_id,
            timeframe=args.timeframe,
            bars=args.bars,
            output_dir=args.output_dir,
        )
        
        client.api_metrics.log_summary()
        return

    if local_backtest_mode:
        # Local backtest from CSV files
        import argparse
        parser = argparse.ArgumentParser(description="Run local backtest from CSV data")
        parser.add_argument("--csv", required=True, help="Path to CSV file with candle data")
        parser.add_argument("--pair", required=True, help="Trading pair (e.g., EURUSD)")
        parser.add_argument("--digits", type=int, required=True, help="Number of decimal places")
        parser.add_argument("--pip-value", type=float, required=True, help="Pip value")
        parser.add_argument("--timeframe", default="M5", help="Timeframe (default: M5)")
        parser.add_argument("--trail-pips", type=float, default=5.0, help="Trailing stop in pips")
        
        args = parser.parse_args(sys.argv[2:])  # Skip script name and --local-backtest
        
        from app.backtest.local_backtest import run_local_backtest_from_csv
        
        logging.info(
            f"🧪 Local Backtest | pair: {args.pair} | CSV: {args.csv} | "
            f"timeframe: {args.timeframe} | digits: {args.digits}"
        )
        
        await run_local_backtest_from_csv(
            csv_file=args.csv,
            pair=args.pair,
            digits=args.digits,
            pip_value=args.pip_value,
            timeframe=args.timeframe,
            trail_pips=args.trail_pips,
        )
        return

    if backtest_mode:
        client = CTraderClient()
        await client.connect()
        await client.authorize()
        await client.init_symbols()

        total_bars = _parse_backtest_bars()
        now = int(time.time())
        logging.info(
            f"🧪 Бэктест Breakout Retest | пары: {', '.join(config.PAIRS)} | "
            f"стратегия: {config.STRATEGY_TYPE} | "
            f"цель ~{total_bars} свечей M5 | "
            f"окно {config.TRADE_WINDOW_START}-{config.TRADE_WINDOW_END} UTC"
        )
        for pair in config.PAIRS:
            pair_cfg = config.get_pair_config(pair)
            entry_tf = pair_cfg.entry_timeframe
            entry_minutes = TIMEFRAME_MINUTES.get(entry_tf, 5)
            period = TIMEFRAME_MAP.get(entry_tf, model_proto.M5)
            bar_ms = entry_minutes * 60 * 1000
            days = total_bars * entry_minutes / 60 / 24
            from_ts = now - total_bars * entry_minutes * 60

            info = client.get_pair_info(pair)
            if not info:
                logging.error(f"❌ Пара {pair} не найдена, пропускаем")
                continue
            symbol_id, _, digits, pip_value, _, _ = info
            logging.info(
                f"📊 Бэктест {pair} ({entry_tf}, ~{days:.0f} дн.) | {pair_cfg}"
            )
            candles = await client.get_trendbars_chunked(
                symbol_id,
                period,
                from_ts * 1000,
                (now - 60) * 1000,
                bar_ms=bar_ms,
                pair=pair,
                timeframe=entry_tf,
            )

            await run_backtest_v3(
                candles,
                digits,
                timeframe=entry_tf,
                pair=pair,
                pip_value=pip_value,
                pair_config=pair_cfg,
                trail_pips=pair_cfg.v3_trail_pips,
            )
        client.api_metrics.log_summary()
        return

    logging.info(
        f"📊 Торгуем по парам: {', '.join(config.PAIRS)} | режим: {config.TRADING_MODE}"
    )
    await _run_live_loop(config)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🛑 Остановлено вручную.")
