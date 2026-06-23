"""
app/connection/ctrader_client.py
TCP/TLS клиент cTrader Open API.
"""

import asyncio
import ssl
import struct
import logging
import math
import uuid
import time
from typing import Optional, List, Dict, Tuple, Callable, Awaitable, Any

import ctrader_open_api.messages.OpenApiMessages_pb2 as proto
import ctrader_open_api.messages.OpenApiCommonMessages_pb2 as common_proto
import ctrader_open_api.messages.OpenApiModelMessages_pb2 as model_proto

from app.config.settings import config
from app.models.candle import Candle
from app.connection.api_metrics import ApiMetrics
from app.trading.volume import (
    PRICE_SCALE,
    ResolvedVolume,
    VOLUME_CENTS_PER_LOT,
    format_volume,
    pip_value_from_symbol,
    price_from_relative,
    relative_stop_loss_distance,
    relative_take_profit_distance,
    resolve_order_volume,
    volume_cents_to_lot,
)


class CTraderRateLimitError(Exception):
    """cTrader Open API: rate limit / BLOCKED_PAYLOAD_TYPE."""


def _is_rate_limit_message(description: str, error_code: str = "") -> bool:
    text = f"{description} {error_code}".lower()
    return "rate limit" in text or "blocked_payload" in text


# ── Retry decorator with exponential backoff ─────────────────────────

def retry_on_ratelimit(max_retries: int = 3, base_delay: float = 1.0):
    """Decorator for retrying functions on rate limiting errors."""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    # Check if it's a rate limiting error
                    error_msg = str(e)
                    if (
                        isinstance(e, CTraderRateLimitError)
                        or "BLOCKED_PAYLOAD_TYPE" in error_msg
                        or "rate limited" in error_msg.lower()
                    ):
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            logging.warning(f"⏳ Rate limited, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})")
                            await asyncio.sleep(delay)
                            continue
                    # Re-raise if not rate limit or max retries exceeded
                    raise
        return wrapper
    return decorator


# symbol_id, price_scale, digits, pip_value, min_volume_cents, step_volume_cents, lot_size_cents
PairInfo = Tuple[int, int, int, float, int, int, int]

# ── Правильные payloadType константы ────────────────────────────────────────
_PT_APP_AUTH_REQ      = model_proto.PROTO_OA_APPLICATION_AUTH_REQ   # 2100
_PT_APP_AUTH_RES      = model_proto.PROTO_OA_APPLICATION_AUTH_RES   # 2101
_PT_ACCT_AUTH_REQ     = model_proto.PROTO_OA_ACCOUNT_AUTH_REQ       # 2102
_PT_ACCT_AUTH_RES     = model_proto.PROTO_OA_ACCOUNT_AUTH_RES       # 2103
_PT_NEW_ORDER_REQ     = model_proto.PROTO_OA_NEW_ORDER_REQ          # 2106
_PT_SYMBOLS_LIST_REQ  = model_proto.PROTO_OA_SYMBOLS_LIST_REQ       # 2114
_PT_SYMBOLS_LIST_RES  = model_proto.PROTO_OA_SYMBOLS_LIST_RES       # 2115
_PT_SYMBOL_BY_ID_REQ  = model_proto.PROTO_OA_SYMBOL_BY_ID_REQ      # 2116
_PT_SYMBOL_BY_ID_RES  = model_proto.PROTO_OA_SYMBOL_BY_ID_RES      # 2117
_PT_AMEND_POS_SLTP_REQ = model_proto.PROTO_OA_AMEND_POSITION_SLTP_REQ  # 2110
_PT_CLOSE_POSITION_REQ = model_proto.PROTO_OA_CLOSE_POSITION_REQ      # 2111
_PT_EXECUTION_EVENT   = model_proto.PROTO_OA_EXECUTION_EVENT        # 2126
_PT_TRENDBARS_REQ     = model_proto.PROTO_OA_GET_TRENDBARS_REQ      # 2137
_PT_TRENDBARS_RES     = model_proto.PROTO_OA_GET_TRENDBARS_RES      # 2138
_PT_ERROR_RES         = model_proto.PROTO_OA_ERROR_RES              # 2142
_PT_TRADER_REQ        = model_proto.PROTO_OA_TRADER_REQ             # 2121
_PT_TRADER_RES        = model_proto.PROTO_OA_TRADER_RES             # 2122
_PT_RECONCILE_REQ     = model_proto.PROTO_OA_RECONCILE_REQ          # 2124
_PT_RECONCILE_RES     = model_proto.PROTO_OA_RECONCILE_RES          # 2125
_PT_TRADER_UPDATE     = model_proto.PROTO_OA_TRADER_UPDATE_EVENT    # 2123
_PT_CANCEL_ORDER_REQ  = model_proto.PROTO_OA_CANCEL_ORDER_REQ       # 2108
_PT_SUBSCRIBE_SPOTS_REQ = model_proto.PROTO_OA_SUBSCRIBE_SPOTS_REQ # 2127
_PT_SUBSCRIBE_SPOTS_RES = model_proto.PROTO_OA_SUBSCRIBE_SPOTS_RES # 2128
_PT_SPOT_EVENT          = model_proto.PROTO_OA_SPOT_EVENT          # 2131
_PT_DEAL_LIST_BY_POSITION_RES = model_proto.PROTO_OA_DEAL_LIST_BY_POSITION_ID_RES  # 2180
_PT_EXPECTED_MARGIN_REQ = model_proto.PROTO_OA_EXPECTED_MARGIN_REQ  # 2139
_PT_EXPECTED_MARGIN_RES = model_proto.PROTO_OA_EXPECTED_MARGIN_RES  # 2140
_PT_HEARTBEAT         = 51

# Пакеты без логирования в listen_loop (высокая частота / разовые при старте)
_SILENT_PACKET_TYPES = frozenset({
    _PT_HEARTBEAT,
    _PT_APP_AUTH_RES,
    _PT_ACCT_AUTH_RES,
    _PT_SYMBOLS_LIST_RES,
    _PT_SYMBOL_BY_ID_RES,
    _PT_TRADER_UPDATE,
    _PT_RECONCILE_RES,
    _PT_EXECUTION_EVENT,
    _PT_DEAL_LIST_BY_POSITION_RES,
    _PT_SUBSCRIBE_SPOTS_RES,
    _PT_SPOT_EVENT,
    _PT_TRENDBARS_RES,
    _PT_EXPECTED_MARGIN_RES,
})

_EXECUTION_TYPE_LABELS = {
    model_proto.ORDER_ACCEPTED: "ORDER_ACCEPTED",
    model_proto.ORDER_FILLED: "ORDER_FILLED",
    model_proto.ORDER_REPLACED: "ORDER_REPLACED",
    model_proto.ORDER_EXPIRED: "ORDER_EXPIRED",
    model_proto.ORDER_REJECTED: "ORDER_REJECTED",
    model_proto.ORDER_CANCELLED: "ORDER_CANCELLED",
}


def _execution_type_label(exec_type: int) -> str:
    return _EXECUTION_TYPE_LABELS.get(exec_type, f"TYPE_{exec_type}")


def _money_to_float(raw: int, money_digits: int) -> float:
    """Конвертирует monetary int из cTrader API в float."""
    return raw / (10 ** money_digits)


def _close_detail_net_pnl(cpd, money_digits: int) -> float:
    """Net P/L из ProtoOAClosePositionDetail (gross + swap + commission)."""
    gross = int(getattr(cpd, "grossProfit", 0) or 0)
    swap = int(getattr(cpd, "swap", 0) or 0)
    comm = int(getattr(cpd, "commission", 0) or 0)
    md = int(getattr(cpd, "moneyDigits", 0) or money_digits)
    return _money_to_float(gross + swap + comm, md)


class CTraderClient:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.app_auth_event  = asyncio.Event()
        self.acct_auth_event = asyncio.Event()

        self._pairs: Dict[str, PairInfo] = {}
        self._symbols_list_event = asyncio.Event()
        self._pending_details: Dict[int, str] = {}
        self._details_events: Dict[str, asyncio.Event] = {}
        self._target_pairs: Optional[set] = None

        self._trendbars_futures: Dict[int, asyncio.Future] = {}
        self._trendbars_lock = asyncio.Lock()

        # Баланс
        self._trader_future: Optional[asyncio.Future] = None

        # Expected Margin
        self._expected_margin_future: Optional[asyncio.Future] = None

        # Reconcile (позиции + pending ордера)
        self._reconcile_future: Optional[asyncio.Future] = None
        self._reconcile_log_key: Optional[Tuple[int, int]] = None
        self._reconcile_cache_at: float = 0.0
        self._reconcile_cache_data: Optional[dict] = None
        self._reconcile_cache_ttl_sec: float = 3.0

        # Ордера: clientOrderId → Future
        self._order_futures: Dict[str, asyncio.Future] = {}
        # clientOrderId -> (stop_loss, pair) — SL на лимитке до fill
        self._order_protection: Dict[str, Tuple[float, str]] = {}
        # clientOrderId -> broker orderId (для отмены)
        self._broker_order_ids: Dict[str, int] = {}
        self._broker_to_client_order_id: Dict[int, str] = {}
        # broker orderId -> Future (ожидание ORDER_CANCELLED)
        self._cancel_futures: Dict[int, asyncio.Future] = {}
        # positionId → Future (amend SL/TP или partial close)
        self._position_op_futures: Dict[int, asyncio.Future] = {}
        self._execution_callback: Optional[Callable[..., Awaitable[Any]]] = None
        self._money_digits: int = 2
        self._deal_list_futures: Dict[int, asyncio.Future] = {}
        self._trendbars_timeframes: Dict[int, str] = {}

        self._compat_pair: Optional[str] = None
        self._market_cache = None
        self._symbol_by_id_future: Optional[asyncio.Future] = None
        self._api_backoff_until: float = 0.0
        self._api_backoff_sec: float = 0.0
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._closing = False
        self._api_metrics = ApiMetrics()

    @property
    def api_metrics(self) -> ApiMetrics:
        return self._api_metrics

    @property
    def symbol_id(self) -> int:
        pair = self._compat_pair or (config.PAIRS[0] if config.PAIRS else None)
        return self._pairs[pair][0] if pair and pair in self._pairs else None

    @property
    def price_multiplier(self) -> int:
        pair = self._compat_pair or (config.PAIRS[0] if config.PAIRS else None)
        return self._pairs[pair][1] if pair and pair in self._pairs else 100000

    @property
    def digits(self) -> int:
        pair = self._compat_pair or (config.PAIRS[0] if config.PAIRS else None)
        return self._pairs[pair][2] if pair and pair in self._pairs else 5

    # ── Подключение ──────────────────────────────────────────────

    async def connect(self):
        logging.info(f"🌐 Подключение к {config.CTRADER_HOST}:{config.CTRADER_PORT}...")
        ctx = ssl.create_default_context()
        self.reader, self.writer = await asyncio.open_connection(
            config.CTRADER_HOST, config.CTRADER_PORT, ssl=ctx
        )
        self._closing = False
        self._listen_task = asyncio.create_task(self._listen_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logging.info("✅ TCP/TLS соединение установлено.")

    async def disconnect(self):
        """Закрывает соединение и останавливает фоновые задачи без шума в логах."""
        self._closing = True
        for task in (self._heartbeat_task, self._listen_task):
            if task and not task.done():
                task.cancel()
        for task in (self._listen_task, self._heartbeat_task):
            if not task:
                continue
            try:
                await task
            except (asyncio.CancelledError, asyncio.IncompleteReadError):
                pass
            except Exception:
                pass
        self._listen_task = None
        self._heartbeat_task = None
        try:
            if self.writer and not self.writer.is_closing():
                self.writer.close()
                await self.writer.wait_closed()
        except Exception:
            pass  # SSL close_notify ошибки при закрытии — игнорируем
        self.reader = None
        self.writer = None

    async def authorize(self):
        await self._send(_PT_APP_AUTH_REQ, proto.ProtoOAApplicationAuthReq(
            clientId=config.CLIENT_ID, clientSecret=config.CLIENT_SECRET))
        await asyncio.wait_for(self.app_auth_event.wait(), timeout=10)
        logging.info("✅ Авторизация приложения успешна.")

        await self._send(_PT_ACCT_AUTH_REQ, proto.ProtoOAAccountAuthReq(
            ctidTraderAccountId=config.ACCOUNT_ID, accessToken=config.ACCESS_TOKEN))
        await asyncio.wait_for(self.acct_auth_event.wait(), timeout=10)
        logging.info("✅ Авторизация аккаунта успешна.")

    # ── Инициализация символов ───────────────────────────────────

    async def init_symbols(self, pairs: Optional[List[str]] = None) -> None:
        target = pairs or config.PAIRS
        self._target_pairs = set(target)

        _FALLBACK_PIPS = {
            'EURUSD': 0.0001, 'GBPUSD': 0.0001,
            'USDJPY': 0.01,
            'USDCHF': 0.0001, 'AUDUSD': 0.0001, 'NZDUSD': 0.0001, 'USDCAD': 0.0001,
            'XAUUSD': 0.01, 'BTCUSD': 0.01,
        }
        _FALLBACK_DIGITS = {k: (3 if v == 0.01 else 5) for k, v in _FALLBACK_PIPS.items()}
        # 100_000 cents = 0.01 lot (cTrader Open API)
        _FALLBACK_MIN_VOLUME_CENTS = 100_000

        await self._send(_PT_SYMBOLS_LIST_REQ, proto.ProtoOASymbolsListReq(
            ctidTraderAccountId=config.ACCOUNT_ID, includeArchivedSymbols=False))
        await asyncio.wait_for(self._symbols_list_event.wait(), timeout=10)

        symbol_ids: Dict[str, int] = {}
        for pair in target:
            if pair not in self._pending_details.values():
                logging.warning(f"⚠️  Символ {pair} не найден на счёте")
                continue
            symbol_ids[pair] = next(
                sid for sid, name in self._pending_details.items() if name == pair
            )

        symbols_by_id = await self._fetch_symbols_by_id(list(symbol_ids.values()))

        for pair, symbol_id in symbol_ids.items():
            sym = symbols_by_id.get(symbol_id)
            if sym is not None:
                digits = int(sym.digits)
                pip_value = pip_value_from_symbol(
                    digits, int(sym.pipPosition), pair=pair,
                )
                price_scale = PRICE_SCALE
                min_volume = int(sym.minVolume) if sym.minVolume > 0 else _FALLBACK_MIN_VOLUME_CENTS
                step_volume = int(sym.stepVolume) if sym.stepVolume > 0 else 0
                lot_size = int(sym.lotSize) if sym.lotSize > 0 else VOLUME_CENTS_PER_LOT
                source = "API"
            elif pair in _FALLBACK_PIPS:
                pip_value = _FALLBACK_PIPS[pair]
                digits = _FALLBACK_DIGITS[pair]
                price_scale = PRICE_SCALE
                min_volume = _FALLBACK_MIN_VOLUME_CENTS
                step_volume = 0
                lot_size = VOLUME_CENTS_PER_LOT
                source = "fallback"
            else:
                logging.error(f"❌ Нет данных для {pair}")
                continue

            self._pairs[pair] = (
                symbol_id, price_scale, digits, pip_value, min_volume, step_volume, lot_size,
            )
            logging.info(
                f"✅ {pair} | ID: {symbol_id} | {source} | "
                f"Digits: {digits} | Pip: {pip_value:.5f} | "
                f"MinVol: {format_volume(min_volume, lot_size)} | "
                f"StepVol: {format_volume(step_volume, lot_size) if step_volume else '—'} | "
                f"LotSize: {lot_size} cents"
            )

        if config.PAIRS:
            self._compat_pair = config.PAIRS[0]
        self._target_pairs = None

    async def _fetch_symbols_by_id(self, symbol_ids: List[int]) -> Dict[int, model_proto.ProtoOASymbol]:
        """Загружает полные спецификации символов (minVolume, digits, pipPosition)."""
        if not symbol_ids:
            return {}

        loop = asyncio.get_running_loop()
        self._symbol_by_id_future = loop.create_future()

        await self._send(_PT_SYMBOL_BY_ID_REQ, proto.ProtoOASymbolByIdReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            symbolId=symbol_ids,
        ))

        try:
            return await asyncio.wait_for(self._symbol_by_id_future, timeout=15)
        except asyncio.TimeoutError:
            logging.warning(
                "⚠️ ProtoOASymbolByIdReq: таймаут — minVolume/digits из fallback"
            )
            return {}
        finally:
            self._symbol_by_id_future = None

    async def init_symbol(self) -> None:
        await self.init_symbols([config.PAIRS[0]])

    def get_pair_info(self, pair: str) -> Optional[PairInfo]:
        return self._pairs.get(pair)

    def _pair_digits(self, pair: str) -> int:
        info = self.get_pair_info(pair) if pair else None
        if info and len(info) > 2:
            return int(info[2])
        return 5

    def _pair_lot_size_cents(self, pair: str) -> int:
        info = self.get_pair_info(pair) if pair else None
        if info and len(info) > 6:
            return int(info[6]) or VOLUME_CENTS_PER_LOT
        return VOLUME_CENTS_PER_LOT

    # ── Баланс счёта ─────────────────────────────────────────────

    async def get_balance(self) -> Optional[float]:
        """Возвращает баланс счёта в валюте депозита."""
        loop = asyncio.get_running_loop()

        # Защита от параллельных вызовов — переиспользуем незавершённый future
        if self._trader_future and not self._trader_future.done():
            try:
                return await asyncio.wait_for(self._trader_future, timeout=10)
            except asyncio.TimeoutError:
                logging.error("❌ Таймаут при ожидании баланса (параллельный вызов)")
                return None

        self._trader_future = loop.create_future()
        await self._send(_PT_TRADER_REQ, proto.ProtoOATraderReq(
            ctidTraderAccountId=config.ACCOUNT_ID))

        try:
            balance = await asyncio.wait_for(self._trader_future, timeout=10)
            return balance
        except asyncio.TimeoutError:
            logging.error("❌ Таймаут при получении баланса")
            return None

    async def get_expected_margin(self, symbol_id: int, volume_cents: int) -> Optional[float]:
        """
        Возвращает оценку маржи для символа и объёма в валюте депозита.
        
        Args:
            symbol_id: ID символа
            volume_cents: объём в центах API (100_000 = 0.01 lot)
            
        Returns:
            Маржа в валюте депозита или None при ошибке
        """
        loop = asyncio.get_running_loop()

        # Защита от параллельных вызовов — переиспользуем незавершённый future
        if self._expected_margin_future and not self._expected_margin_future.done():
            try:
                return await asyncio.wait_for(self._expected_margin_future, timeout=10)
            except asyncio.TimeoutError:
                logging.error("❌ Таймаут при ожидании expected margin (параллельный вызов)")
                return None

        self._expected_margin_future = loop.create_future()
        await self._send(_PT_EXPECTED_MARGIN_REQ, proto.ProtoOAExpectedMarginReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            symbolId=symbol_id,
            volume=[volume_cents],
        ))

        try:
            margin = await asyncio.wait_for(self._expected_margin_future, timeout=10)
            return margin
        except asyncio.TimeoutError:
            logging.error("❌ Таймаут при получении expected margin")
            return None
        finally:
            self._expected_margin_future = None

    # ── Размещение ордера ─────────────────────────────────────────

    async def place_limit_order(
        self,
        symbol_id:   int,
        direction,
        lot:         float,
        entry:       float,
        stop_loss:   float,
        multiplier:  int,
        min_volume:  int = 100_000,
        step_volume: int = 0,
        pair:        str = "",
    ) -> Optional[tuple[str, ResolvedVolume]]:
        """
        Размещает limit-ордер только с SL (без TP).
        При TRAILING_STOP_LOSS=true — trailingStopLoss в ProtoOANewOrderReq.
        Возвращает (clientOrderId, ResolvedVolume) при успехе, None при ошибке.
        """
        loop            = asyncio.get_running_loop()
        client_order_id = str(uuid.uuid4())
        fut             = loop.create_future()
        self._order_futures[client_order_id] = fut

        # Handle both string directions ("BUY"/"SELL") and Direction enum
        trade_side = model_proto.BUY if str(direction).upper() in ("BUY", "LONG") else model_proto.SELL

        resolved = resolve_order_volume(
            lot, min_volume, step_volume, lot_size_cents=self._pair_lot_size_cents(pair),
        )
        volume = resolved.actual_volume_cents
        digits = self._pair_digits(pair)
        entry = round(float(entry), digits)
        stop_loss = round(float(stop_loss), digits)
        rel_sl = relative_stop_loss_distance(entry, stop_loss, digits=digits)

        pair_tag = f"[{pair}] " if pair else ""
        logging.debug(
            f"📤 {pair_tag}place_limit_order: symbol_id={symbol_id} side={trade_side} | "
            f"vol={format_volume(volume, resolved.lot_size_cents)}"
            + (f" | min↑{min_volume}" if resolved.bumped_to_min else "")
            + (f" | step={step_volume}" if resolved.rounded_to_step else "")
            + f" | entry={entry} sl={stop_loss} rel_sl={rel_sl}"
            + (f" | trailing_sl={config.TRAILING_STOP_LOSS}" if config.TRAILING_STOP_LOSS else "")
        )

        # Только relative SL — брокер отклоняет absolute + relative вместе.
        max_age_sec = int(config.PENDING_ORDER_MAX_AGE_SECONDS)
        expiration_ms = int(time.time() * 1000) + max_age_sec * 1000
        req = proto.ProtoOANewOrderReq(
            ctidTraderAccountId = config.ACCOUNT_ID,
            symbolId            = symbol_id,
            orderType           = model_proto.LIMIT,
            tradeSide           = trade_side,
            volume              = volume,
            limitPrice          = entry,
            relativeStopLoss    = rel_sl,
            clientOrderId       = client_order_id,
            timeInForce         = model_proto.GOOD_TILL_DATE,
            expirationTimestamp = expiration_ms,
        )
        if config.TRAILING_STOP_LOSS:
            req.trailingStopLoss = True
        logging.debug(
            f"📤 {pair_tag}limit TTL: GOOD_TILL_DATE expires in {max_age_sec}s "
            f"(ts={expiration_ms})"
        )
        self._order_protection[client_order_id] = (float(stop_loss), pair)

        await self._send(_PT_NEW_ORDER_REQ, req)

        try:
            order_id = await asyncio.wait_for(fut, timeout=10)
            if order_id:
                return order_id, resolved
            self._order_protection.pop(client_order_id, None)
            return None
        except asyncio.TimeoutError:
            logging.error("❌ Таймаут при размещении ордера")
            self._order_futures.pop(client_order_id, None)
            self._order_protection.pop(client_order_id, None)
            return None

    async def place_stop_order(
        self,
        symbol_id:   int,
        direction,
        lot:         float,
        entry:       float,
        stop_loss:   float,
        multiplier:  int,
        min_volume:  int = 100_000,
        step_volume: int = 0,
        pair:        str = "",
        *,
        trailing_sl: Optional[bool] = None,
        take_profit: Optional[float] = None,
    ) -> Optional[tuple[str, ResolvedVolume]]:
        """
        Размещает stop-ордер (Buy Stop / Sell Stop) с relative SL.
        take_profit: optional relativeTakeProfit (только relative, не вместе с absolute).
        trailing_sl: None → config.TRAILING_STOP_LOSS.
        """
        loop            = asyncio.get_running_loop()
        client_order_id = str(uuid.uuid4())
        fut             = loop.create_future()
        self._order_futures[client_order_id] = fut

        trade_side = model_proto.BUY if str(direction).upper() in ("BUY", "LONG") else model_proto.SELL

        resolved = resolve_order_volume(
            lot, min_volume, step_volume, lot_size_cents=self._pair_lot_size_cents(pair),
        )
        volume = resolved.actual_volume_cents
        digits = self._pair_digits(pair)
        entry = round(float(entry), digits)
        stop_loss = round(float(stop_loss), digits)
        rel_sl = relative_stop_loss_distance(entry, stop_loss, digits=digits)
        rel_tp = None
        if take_profit is not None and take_profit > 0:
            take_profit = round(float(take_profit), digits)
            rel_tp = relative_take_profit_distance(entry, take_profit, digits=digits)

        pair_tag = f"[{pair}] " if pair else ""
        tp_dbg = f" rel_tp={take_profit}" if take_profit else ""
        logging.debug(
            f"📤 {pair_tag}place_stop_order: symbol_id={symbol_id} side={trade_side} | "
            f"vol={format_volume(volume, resolved.lot_size_cents)}"
            + (f" | min↑{min_volume}" if resolved.bumped_to_min else "")
            + (f" | step={step_volume}" if resolved.rounded_to_step else "")
            + f" | stop={entry} sl={stop_loss} rel_sl={rel_sl}{tp_dbg}"
            + (
                f" | trailing_sl={trailing_sl}"
                if trailing_sl is not None
                else (
                    f" | trailing_sl={config.TRAILING_STOP_LOSS}"
                    if config.TRAILING_STOP_LOSS else ""
                )
            )
        )

        use_trailing = (
            config.TRAILING_STOP_LOSS if trailing_sl is None else trailing_sl
        )
        max_age_sec = int(config.PENDING_ORDER_MAX_AGE_SECONDS)
        expiration_ms = int(time.time() * 1000) + max_age_sec * 1000
        req = proto.ProtoOANewOrderReq(
            ctidTraderAccountId = config.ACCOUNT_ID,
            symbolId            = symbol_id,
            orderType           = model_proto.STOP,
            tradeSide           = trade_side,
            volume              = volume,
            stopPrice           = entry,
            relativeStopLoss    = rel_sl,
            clientOrderId       = client_order_id,
            timeInForce         = model_proto.GOOD_TILL_DATE,
            expirationTimestamp = expiration_ms,
        )
        if rel_tp is not None:
            req.relativeTakeProfit = rel_tp
        if use_trailing:
            req.trailingStopLoss = True
        logging.debug(
            f"📤 {pair_tag}stop TTL: GOOD_TILL_DATE expires in {max_age_sec}s "
            f"(ts={expiration_ms})"
        )
        self._order_protection[client_order_id] = (float(stop_loss), pair)

        await self._send(_PT_NEW_ORDER_REQ, req)

        try:
            order_id = await asyncio.wait_for(fut, timeout=10)
            if order_id:
                return order_id, resolved
            self._order_protection.pop(client_order_id, None)
            return None
        except asyncio.TimeoutError:
            logging.error("❌ Таймаут при размещении stop-ордера")
            self._order_futures.pop(client_order_id, None)
            self._order_protection.pop(client_order_id, None)
            return None

    async def place_position_close_limit(
        self,
        symbol_id: int,
        position_id: int,
        position_direction: str,
        volume_cents: int,
        limit_price: float,
        *,
        min_volume: int = 100_000,
        step_volume: int = 0,
        pair: str = "",
        digits: int = 5,
        timeout: float = 10.0,
    ) -> Optional[str]:
        """Limit-ордер на закрытие части позиции по TP (positionId)."""
        if volume_cents <= 0 or not position_id:
            return None

        close_side = (
            model_proto.SELL
            if str(position_direction).upper() in ("BUY", "LONG")
            else model_proto.BUY
        )
        client_order_id = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self._order_futures[client_order_id] = fut

        pair_tag = f"[{pair}] " if pair else ""
        logging.debug(
            f"📤 {pair_tag}position TP limit: pos={position_id} side={close_side} "
            f"vol={volume_cents} @ {limit_price}"
        )

        req = proto.ProtoOANewOrderReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            symbolId=symbol_id,
            orderType=model_proto.LIMIT,
            tradeSide=close_side,
            volume=int(volume_cents),
            limitPrice=round(float(limit_price), digits),
            positionId=int(position_id),
            clientOrderId=client_order_id,
            timeInForce=model_proto.GOOD_TILL_CANCEL,
        )
        await self._send(_PT_NEW_ORDER_REQ, req)

        try:
            order_id = await asyncio.wait_for(fut, timeout=timeout)
            return order_id if order_id else None
        except asyncio.TimeoutError:
            logging.error(f"❌ [{pair}] Таймаут TP limit @ {limit_price}")
            self._order_futures.pop(client_order_id, None)
            return None

    def set_execution_callback(
        self,
        callback: Optional[Callable[..., Awaitable[Any]]],
    ) -> None:
        """Async callback(exec_type, client_order_id, pair, position_id)."""
        self._execution_callback = callback

    def get_broker_order_id(self, client_order_id: str) -> Optional[int]:
        return self._broker_order_ids.get(client_order_id)

    def _unregister_broker_order(self, client_order_id: Optional[str]) -> None:
        if not client_order_id:
            return
        broker_id = self._broker_order_ids.pop(client_order_id, None)
        if broker_id is not None:
            self._broker_to_client_order_id.pop(int(broker_id), None)

    def _client_id_from_order_error(self, res: Any) -> Optional[str]:
        """ProtoOAOrderErrorEvent: orderId, без clientOrderId."""
        if not res.HasField("orderId"):
            return None
        broker_id = int(res.orderId)
        client_id = self._broker_to_client_order_id.get(broker_id)
        if client_id:
            return client_id
        for cid, bid in self._broker_order_ids.items():
            if bid == broker_id:
                return cid
        return None

    def _pair_for_symbol_id(self, symbol_id: Optional[int]) -> str:
        if not symbol_id:
            return ""
        for pair_name, pair_info in self._pairs.items():
            if pair_info[0] == symbol_id:
                return pair_name
        return ""

    def _parse_reconcile_position(self, pos) -> dict:
        trade_data = getattr(pos, "tradeData", None)
        if trade_data:
            symbol_id = getattr(trade_data, "symbolId", None)
            symbol_name = self._pair_for_symbol_id(symbol_id)
            volume = getattr(trade_data, "volume", 0)
            trade_side = getattr(trade_data, "tradeSide", None)
            direction = "BUY" if trade_side == model_proto.BUY else "SELL"
        else:
            symbol_name = "UNKNOWN"
            volume = 0
            direction = "UNKNOWN"

        return {
            "id": getattr(pos, "positionId", "unknown"),
            "symbol": symbol_name,
            "volume": volume,
            "entry_price": getattr(pos, "price", 0),
            "current_price": getattr(pos, "price", 0),
            "margin": getattr(pos, "usedMargin", 0),
            "profit": (
                getattr(pos, "unrealizedPnL", None)
                or getattr(pos, "swap", 0)
            ) / 100.0,
            "direction": direction,
        }

    def _parse_reconcile_order(self, ord) -> dict:
        trade_data = getattr(ord, "tradeData", None)
        symbol_id = getattr(trade_data, "symbolId", None) if trade_data else None
        open_ts = int(getattr(trade_data, "openTimestamp", 0) or 0) if trade_data else 0
        last_ts = int(getattr(ord, "utcLastUpdateTimestamp", 0) or 0)
        trade_side = getattr(trade_data, "tradeSide", None) if trade_data else None
        if trade_side == model_proto.BUY:
            direction = "BUY"
        elif trade_side == model_proto.SELL:
            direction = "SELL"
        else:
            direction = ""
        volume = int(getattr(trade_data, "volume", 0) or 0) if trade_data else 0
        return {
            "orderId": int(getattr(ord, "orderId", 0)),
            "clientOrderId": getattr(ord, "clientOrderId", "") or "",
            "pair": self._pair_for_symbol_id(symbol_id),
            "orderType": getattr(ord, "orderType", None),
            "orderStatus": getattr(ord, "orderStatus", None),
            "open_timestamp_ms": open_ts,
            "last_update_ms": last_ts,
            "limitPrice": float(getattr(ord, "limitPrice", 0) or 0),
            "direction": direction,
            "volume": volume,
        }

    async def cancel_order(self, broker_order_id: int, timeout: float = 10.0) -> bool:
        """Отменяет pending-ордер по broker orderId."""
        if not broker_order_id:
            return False

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._cancel_futures[int(broker_order_id)] = fut

        req = proto.ProtoOACancelOrderReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            orderId=int(broker_order_id),
        )
        await self._send(_PT_CANCEL_ORDER_REQ, req)

        try:
            return bool(await asyncio.wait_for(fut, timeout=timeout))
        except asyncio.TimeoutError:
            logging.error(f"❌ Таймаут отмены ордера {broker_order_id}")
            return False
        finally:
            self._cancel_futures.pop(int(broker_order_id), None)

    def register_order_mapping(self, client_order_id: str, broker_order_id: int) -> None:
        """Связать clientOrderId ↔ broker orderId (recovery)."""
        bid = int(broker_order_id)
        self._broker_order_ids[client_order_id] = bid
        self._broker_to_client_order_id[bid] = client_order_id

    async def cancel_order_by_client_id(self, client_order_id: str, timeout: float = 10.0) -> bool:
        broker_id = self._broker_order_ids.get(client_order_id)
        if broker_id:
            return await self.cancel_order(broker_id, timeout=timeout)
        logging.warning(f"⚠️ Нет broker orderId для clientOrderId={client_order_id}")
        return False

    def _complete_position_op(self, position_id: Optional[int], *, ok: bool = True) -> None:
        if not position_id:
            return
        fut = self._position_op_futures.pop(int(position_id), None)
        if fut and not fut.done():
            fut.set_result(ok)

    def _register_position_op(self, position_id: int) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._position_op_futures[int(position_id)] = fut
        return fut

    async def _await_position_op(
        self, position_id: int, fut: asyncio.Future, timeout: float = 10.0,
    ) -> bool:
        try:
            return bool(await asyncio.wait_for(fut, timeout=timeout))
        except asyncio.TimeoutError:
            logging.error(f"❌ Таймаут операции по позиции {position_id}")
            self._position_op_futures.pop(int(position_id), None)
            return False

    async def amend_position_sltp(
        self,
        position_id: int,
        *,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trailing_stop_loss: bool = False,
        digits: int = 5,
        timeout: float = 10.0,
    ) -> bool:
        """Amend SL/TP/trailing на открытой позиции."""
        if not position_id:
            return False

        req = proto.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            positionId=int(position_id),
        )
        if stop_loss is not None:
            req.stopLoss = round(float(stop_loss), digits)
        if take_profit is not None:
            req.takeProfit = round(float(take_profit), digits)
        if trailing_stop_loss:
            req.trailingStopLoss = True

        fut = self._register_position_op(int(position_id))
        await self._send(_PT_AMEND_POS_SLTP_REQ, req)
        return await self._await_position_op(int(position_id), fut, timeout=timeout)

    async def close_position_partial(
        self,
        position_id: int,
        volume_cents: int,
        *,
        timeout: float = 10.0,
    ) -> bool:
        """Частичное закрытие позиции (volume в центах API)."""
        if not position_id or volume_cents <= 0:
            return False

        req = proto.ProtoOAClosePositionReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            positionId=int(position_id),
            volume=int(volume_cents),
        )
        fut = self._register_position_op(int(position_id))
        await self._send(_PT_CLOSE_POSITION_REQ, req)
        return await self._await_position_op(int(position_id), fut, timeout=timeout)

    def position_volume_cents(self, position_id: int, positions: list[dict]) -> Optional[int]:
        for pos in positions:
            if int(pos.get("id", 0) or 0) == int(position_id):
                return int(pos.get("volume", 0) or 0)
        return None

    # ── Загрузка свечей ──────────────────────────────────────────

    async def get_trendbars_chunked(
        self,
        symbol_id: int,
        period: int,
        from_timestamp: int,
        to_timestamp: int,
        bar_ms: int = 5 * 60 * 1000,
        chunk_bars: int = 900,
        pair: str = "UNKNOWN",
        timeframe: str = "UNKNOWN",
        verbose: bool = False,
    ) -> List[Candle]:
        async with self._trendbars_lock:
            return await self._get_trendbars_chunked_unlocked(
                symbol_id,
                period,
                from_timestamp,
                to_timestamp,
                bar_ms=bar_ms,
                chunk_bars=chunk_bars,
                pair=pair,
                timeframe=timeframe,
                verbose=verbose,
            )

    async def _get_trendbars_chunked_unlocked(
        self,
        symbol_id: int,
        period: int,
        from_timestamp: int,
        to_timestamp: int,
        bar_ms: int = 5 * 60 * 1000,
        chunk_bars: int = 900,
        pair: str = "UNKNOWN",
        timeframe: str = "UNKNOWN",
        verbose: bool = False,
    ) -> List[Candle]:
        by_ts:    Dict[int, Candle] = {}
        chunk_num = 0
        to_ts     = to_timestamp
        chunk_ms  = chunk_bars * bar_ms

        multiplier = next(
            (v[1] for v in self._pairs.values() if v[0] == symbol_id),
            self.price_multiplier,
        )
        pair_count = max(1, len(self._pairs))
        first_chunk_delay = 0.15 if pair_count > 1 else 0.0
        inter_chunk_delay = 0.5
        if pair_count > 12:
            first_chunk_delay = max(first_chunk_delay, 0.25)
            inter_chunk_delay = 0.65
        elif pair_count > 9:
            inter_chunk_delay = 0.55

        while to_ts > from_timestamp:
            chunk_num += 1
            from_ts_c = max(from_timestamp, to_ts - chunk_ms)

            if chunk_num > 1:
                await asyncio.sleep(inter_chunk_delay)
            elif first_chunk_delay > 0:
                await asyncio.sleep(first_chunk_delay)

            chunk = await self._get_trendbars_raw(
                symbol_id, period, from_ts_c, to_ts, multiplier, timeframe=timeframe,
            )
            self._api_metrics.record_trendbar_chunk()

            if not chunk:
                log = logging.info if verbose else logging.debug
                log(f"📭 [{pair}] {timeframe} пустой чанк — история закончилась")
                break

            added = sum(1 for c in chunk if c.timestamp not in by_ts)
            for c in chunk:
                by_ts[c.timestamp] = c

            if verbose and chunk_num > 1:
                logging.info(
                    f"📥 [{pair}] {timeframe} чанк #{chunk_num}: +{added} | всего {len(by_ts)}"
                )

            oldest_ts = min(c.timestamp for c in chunk)
            if oldest_ts <= from_timestamp:
                break
            if len(chunk) < 10:
                break
            to_ts = oldest_ts

        candles = sorted(by_ts.values(), key=lambda c: c.timestamp)
        if verbose:
            if chunk_num > 1:
                logging.info(
                    f"📦 [{pair}] {timeframe} итого: {len(candles)} свечей ({chunk_num} чанков)"
                )
            else:
                logging.info(f"📦 [{pair}] {timeframe} загружено {len(candles)} свечей")
        return candles

    @retry_on_ratelimit(max_retries=3, base_delay=1.0)
    async def _get_trendbars_raw(
        self, symbol_id, period, from_ts, to_ts, multiplier, timeframe: str = "",
    ):
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        self._trendbars_futures[symbol_id] = fut
        if timeframe:
            self._trendbars_timeframes[symbol_id] = timeframe

        req = proto.ProtoOAGetTrendbarsReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            symbolId=symbol_id,
            period=period,
            fromTimestamp=from_ts,
            toTimestamp=to_ts,
        )
        await self._send(_PT_TRENDBARS_REQ, req)

        try:
            return await asyncio.wait_for(fut, timeout=30)
        except CTraderRateLimitError:
            raise
        except asyncio.TimeoutError:
            logging.error(f"❌ Таймаут загрузки свечей symbol_id={symbol_id}")
            return []
        finally:
            self._trendbars_futures.pop(symbol_id, None)

    # ── Scalping методы ─────────────────────────────────────────────

    async def get_spread(self, pair: str) -> Optional[float]:
        """
        Возвращает текущий spread в пунктах для пары (из MarketCache после subscribe_quotes).
        """
        if self._market_cache:
            spread = self._market_cache.get_spread(pair)
            if spread is not None:
                return spread

        logging.debug(f"⚠️ get_spread: нет актуальных котировок в MarketCache для {pair}")
        return None
    
    async def _fetch_reconcile(self) -> dict:
        """Запрашивает Reconcile: открытые позиции и pending-ордера."""
        loop = asyncio.get_running_loop()

        if self._reconcile_future and not self._reconcile_future.done():
            try:
                return await asyncio.wait_for(self._reconcile_future, timeout=10)
            except asyncio.TimeoutError:
                logging.error("❌ Таймаут при ожидании reconcile (параллельный вызов)")
                return {"positions": [], "orders": []}

        self._reconcile_future = loop.create_future()
        await self._send(_PT_RECONCILE_REQ, proto.ProtoOAReconcileReq(
            ctidTraderAccountId=config.ACCOUNT_ID))

        try:
            return await asyncio.wait_for(self._reconcile_future, timeout=10)
        except asyncio.TimeoutError:
            logging.error("❌ Таймаут при reconcile")
            return {"positions": [], "orders": []}
        finally:
            self._reconcile_future = None

    def _filter_pending_orders(self, data: dict) -> list[dict]:
        orders = data.get("orders", [])
        accepted = getattr(model_proto, "ORDER_STATUS_ACCEPTED", 1)
        return [o for o in orders if o.get("orderStatus") == accepted]

    async def fetch_reconcile_snapshot(self, *, force: bool = False) -> dict:
        """
        Один запрос Reconcile. При force=False повтор в течение TTL
        возвращает кэш (меньше гонок с cleanup лимиток).
        """
        now = time.monotonic()
        if (
            not force
            and self._reconcile_cache_data is not None
            and (now - self._reconcile_cache_at) < self._reconcile_cache_ttl_sec
        ):
            return self._reconcile_cache_data

        data = await self._fetch_reconcile()
        self._reconcile_cache_data = data
        self._reconcile_cache_at = now
        return data

    async def get_reconcile_state(
        self, *, force: bool = False,
    ) -> Tuple[list[dict], list[dict]]:
        """Позиции и pending-ордера за один Reconcile."""
        data = await self.fetch_reconcile_snapshot(force=force)
        return data.get("positions", []), self._filter_pending_orders(data)

    async def get_positions(self, *, force: bool = False) -> list[dict]:
        """Открытые позиции (recovery)."""
        positions, _ = await self.get_reconcile_state(force=force)
        return positions

    async def get_pending_orders(self, *, force: bool = False) -> list[dict]:
        """Pending-ордера у брокера (limit/stop, status ACCEPTED)."""
        _, pending = await self.get_reconcile_state(force=force)
        return pending

    async def get_position_close_profit(self, position_id: int) -> Optional[float]:
        """
        Запрашивает сделки по positionId и возвращает net P/L закрытия.
        Fallback когда execution event не содержит grossProfit.
        """
        if not position_id:
            return None

        pid = int(position_id)
        if pid in self._deal_list_futures and not self._deal_list_futures[pid].done():
            try:
                return await asyncio.wait_for(self._deal_list_futures[pid], timeout=10)
            except asyncio.TimeoutError:
                return None

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._deal_list_futures[pid] = fut

        now_ms = int(time.time() * 1000)
        await self._send(
            model_proto.PROTO_OA_DEAL_LIST_BY_POSITION_ID_REQ,
            proto.ProtoOADealListByPositionIdReq(
                ctidTraderAccountId=config.ACCOUNT_ID,
                positionId=pid,
                fromTimestamp=now_ms - 30 * 24 * 3600 * 1000,
                toTimestamp=now_ms,
            ),
        )

        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            logging.warning(f"⚠️ Таймаут DealListByPositionId для position={pid}")
            return None
        finally:
            self._deal_list_futures.pop(pid, None)
    
    async def subscribe_quotes(self, market_cache=None) -> None:
        """
        Подписывается на котировки для обновления MarketCache.
        Обрабатывает ProtoOASpotEvent сообщения.
        
        Args:
            market_cache: Экземпляр MarketCache для обновления (опционально)
        """
        self._market_cache = market_cache
        
        symbol_ids = [info[0] for info in self._pairs.values()]
        
        # ProtoOASubscribeSpotsReq принимает список symbolId (repeated field)
        req = proto.ProtoOASubscribeSpotsReq(
            ctidTraderAccountId=config.ACCOUNT_ID,
            symbolId=symbol_ids,  # Список symbolId
            subscribeToSpotTimestamp=0  # 0 = с текущего момента
        )
        await self._send(_PT_SUBSCRIBE_SPOTS_REQ, req)
        
        logging.info(f"📊 Подписано на котировки для {len(symbol_ids)} символов")

    # ── Внутренние методы ────────────────────────────────────────

    async def _wait_api_backoff(self) -> None:
        remaining = self._api_backoff_until - time.monotonic()
        if remaining > 0:
            logging.warning(f"⏳ cTrader API: ожидание backoff {remaining:.1f}s")
            await asyncio.sleep(remaining)

    def _fail_pending_on_rate_limit(self, description: str) -> None:
        err = CTraderRateLimitError(description)
        for sid, fut in list(self._trendbars_futures.items()):
            if fut and not fut.done():
                fut.set_exception(err)
            self._trendbars_futures.pop(sid, None)
        if self._reconcile_future and not self._reconcile_future.done():
            self._reconcile_future.set_exception(err)
        if self._trader_future and not self._trader_future.done():
            self._trader_future.set_exception(err)

    async def _on_rate_limited(self, description: str) -> None:
        self._api_backoff_sec = min(60.0, max(5.0, self._api_backoff_sec * 2 or 5.0))
        self._api_backoff_until = time.monotonic() + self._api_backoff_sec
        self._api_metrics.record_rate_limit(backoff_sec=self._api_backoff_sec)
        logging.warning(
            f"⏳ cTrader rate limit — пауза {self._api_backoff_sec:.0f}s | {description}"
        )
        self._fail_pending_on_rate_limit(description)

    async def _send(self, payload_type, message):
        await self._wait_api_backoff()
        self._api_metrics.record_send(payload_type)
        if hasattr(message, 'clientMsgId'):
            message.clientMsgId = str(uuid.uuid4())
        wrapper = common_proto.ProtoMessage()
        wrapper.payloadType = payload_type
        wrapper.payload = message.SerializeToString()
        data   = wrapper.SerializeToString()
        header = struct.pack(">I", len(data))
        self.writer.write(header + data)
        await self.writer.drain()

    async def _listen_loop(self):
        try:
            while True:
                header  = await self.reader.readexactly(4)
                msg_len = struct.unpack(">I", header)[0]
                body    = await self.reader.readexactly(msg_len)

                msg = common_proto.ProtoMessage()
                msg.ParseFromString(body)
                pt = msg.payloadType

                if pt not in _SILENT_PACKET_TYPES:
                    logging.debug(f"📨 Получен пакет pt={pt}")

                if pt == _PT_HEARTBEAT:
                    continue

                elif pt == _PT_APP_AUTH_RES:
                    self.app_auth_event.set()

                elif pt == _PT_ACCT_AUTH_RES:
                    self.acct_auth_event.set()

                elif pt == _PT_TRADER_RES:
                    try:
                        res = proto.ProtoOATraderRes()
                        res.ParseFromString(msg.payload)
                        if res.HasField("trader"):
                            md = int(getattr(res.trader, "moneyDigits", 0) or 2)
                            if md > 0:
                                self._money_digits = md
                            balance = _money_to_float(int(res.trader.balance), self._money_digits)
                        else:
                            balance = 0.0
                        logging.debug(f"💰 Баланс счёта: {balance:.2f}")
                        if self._trader_future and not self._trader_future.done():
                            self._trader_future.set_result(balance)
                        
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки TRADER_RES: {e}", exc_info=True)

                elif pt == _PT_EXPECTED_MARGIN_RES:
                    try:
                        res = proto.ProtoOAExpectedMarginRes()
                        res.ParseFromString(msg.payload)
                        money_digits = int(getattr(res, "moneyDigits", 0) or self._money_digits)
                        margin_value = 0.0
                        if res.margin:
                            # Берём buyMargin из первого элемента (для одного объёма)
                            margin_data = res.margin[0]
                            buy_margin = int(getattr(margin_data, "buyMargin", 0) or 0)
                            sell_margin = int(getattr(margin_data, "sellMargin", 0) or 0)
                            # Используем buyMargin как значение маржи
                            margin_value = _money_to_float(buy_margin, money_digits)
                            logging.debug(
                                f"📊 Expected Margin: buy={margin_value:.2f}, "
                                f"sell={_money_to_float(sell_margin, money_digits):.2f}, "
                                f"moneyDigits={money_digits}"
                            )
                        if self._expected_margin_future and not self._expected_margin_future.done():
                            self._expected_margin_future.set_result(margin_value)
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки EXPECTED_MARGIN_RES: {e}", exc_info=True)
                        if self._expected_margin_future and not self._expected_margin_future.done():
                            self._expected_margin_future.set_result(None)

                elif pt == _PT_DEAL_LIST_BY_POSITION_RES:
                    try:
                        res = proto.ProtoOADealListByPositionIdRes()
                        res.ParseFromString(msg.payload)
                        net_pnl = 0.0
                        has_close = False
                        for deal in getattr(res, "deal", None) or []:
                            if deal.HasField("closePositionDetail"):
                                has_close = True
                                net_pnl += _close_detail_net_pnl(
                                    deal.closePositionDetail, self._money_digits,
                                )
                        pid = None
                        deals = getattr(res, "deal", None) or []
                        if deals:
                            pid = int(getattr(deals[0], "positionId", 0) or 0)
                        profit = net_pnl if has_close else None
                        if pid and pid in self._deal_list_futures:
                            fut = self._deal_list_futures.get(pid)
                            if fut and not fut.done():
                                fut.set_result(profit)
                    except Exception as e:
                        logging.error(
                            f"❌ Ошибка обработки DEAL_LIST_BY_POSITION_ID_RES: {e}",
                            exc_info=True,
                        )

                elif pt == _PT_RECONCILE_RES:
                    try:
                        res = proto.ProtoOAReconcileRes()
                        res.ParseFromString(msg.payload)

                        if self._reconcile_future and not self._reconcile_future.done():
                            positions = [
                                self._parse_reconcile_position(pos)
                                for pos in (getattr(res, "position", None) or [])
                            ]
                            orders = [
                                self._parse_reconcile_order(ord)
                                for ord in (getattr(res, "order", None) or [])
                            ]
                            log_key = (len(positions), len(orders))
                            if log_key != self._reconcile_log_key:
                                self._reconcile_log_key = log_key
                                logging.debug(
                                    f"📊 Reconcile: {log_key[0]} позиций, "
                                    f"{log_key[1]} pending-ордеров"
                                )
                            self._reconcile_future.set_result({
                                "positions": positions,
                                "orders": orders,
                            })
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки RECONCILE_RES: {e}", exc_info=True)

                elif pt == _PT_SYMBOLS_LIST_RES:
                    try:
                        res = proto.ProtoOASymbolsListRes()
                        res.ParseFromString(msg.payload)
                        target = self._target_pairs or set(config.PAIRS)
                        for s in res.symbol:
                            if s.symbolName in target:
                                self._pending_details[s.symbolId] = s.symbolName
                                self._details_events[s.symbolName] = asyncio.Event()
                                logging.info(f"🎯 Найден {s.symbolName} (ID: {s.symbolId})")
                        self._symbols_list_event.set()
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки SYMBOLS_LIST_RES: {e}", exc_info=True)
                        self._symbols_list_event.set()

                elif pt == _PT_SYMBOL_BY_ID_RES:
                    try:
                        res = proto.ProtoOASymbolByIdRes()
                        res.ParseFromString(msg.payload)
                        by_id = {int(s.symbolId): s for s in res.symbol}
                        logging.info(f"📋 SymbolById: получено {len(by_id)} спецификаций")
                        if self._symbol_by_id_future and not self._symbol_by_id_future.done():
                            self._symbol_by_id_future.set_result(by_id)
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки SYMBOL_BY_ID_RES: {e}", exc_info=True)
                        if self._symbol_by_id_future and not self._symbol_by_id_future.done():
                            self._symbol_by_id_future.set_result({})

                elif pt == _PT_TRENDBARS_RES:
                    try:
                        res = proto.ProtoOAGetTrendbarsRes()
                        res.ParseFromString(msg.payload)
                        sid = res.symbolId
                        digits = next(
                            (v[2] for v in self._pairs.values() if v[0] == sid), 5,
                        )
                        candles = []
                        for t in res.trendbar:
                            ts = t.utcTimestampInMinutes * 60 * 1000
                            low = price_from_relative(t.low, digits)
                            open_ = price_from_relative(
                                int(t.low) + int(t.deltaOpen), digits,
                            )
                            close = price_from_relative(
                                int(t.low) + int(t.deltaClose), digits,
                            )
                            high = price_from_relative(
                                int(t.low) + int(t.deltaHigh), digits,
                            )
                            candles.append(Candle(ts, open_, high, low, close, t.volume))
                        self._trendbars_timeframes.pop(sid, None)
                        fut = self._trendbars_futures.get(sid)
                        if fut and not fut.done():
                            fut.set_result(candles)
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки TRENDBARS_RES: {e}", exc_info=True)

                elif pt == _PT_EXECUTION_EVENT:
                    try:
                        res = proto.ProtoOAExecutionEvent()
                        res.ParseFromString(msg.payload)
                        exec_type = res.executionType

                        order_id = res.order.clientOrderId if res.HasField("order") else None
                        broker_order_id = None
                        if res.HasField("order"):
                            broker_order_id = getattr(res.order, "orderId", None)

                        fut = self._order_futures.pop(order_id, None) if order_id else None
                        position_id = None
                        pair_name = ""
                        if order_id and order_id in self._order_protection:
                            pair_name = self._order_protection[order_id][1]
                        if not pair_name and res.HasField("order"):
                            td = getattr(res.order, "tradeData", None)
                            if td:
                                pair_name = self._pair_for_symbol_id(getattr(td, "symbolId", None))

                        if exec_type == model_proto.ORDER_ACCEPTED:
                            if order_id and broker_order_id:
                                bid = int(broker_order_id)
                                self._broker_order_ids[order_id] = bid
                                self._broker_to_client_order_id[bid] = order_id
                            if fut and not fut.done():
                                logging.debug(f"✅ Ордер принят брокером: {order_id}")
                                fut.set_result(order_id)

                        elif exec_type == model_proto.ORDER_FILLED:
                            if order_id:
                                self._order_protection.pop(order_id, None)
                            if res.HasField("position"):
                                position_id = getattr(
                                    res.position, "positionId",
                                    getattr(res.position, "position_id", None),
                                )
                                sym_id = getattr(res.position, "symbolId", None)
                                if sym_id:
                                    pair_name = pair_name or self._pair_for_symbol_id(sym_id)
                                if position_id and pair_name:
                                    logging.info(
                                        f"✅ [{pair_name}] Лимит исполнен → позиция "
                                        f"id={position_id} (order={order_id})"
                                    )

                            if fut and not fut.done():
                                logging.debug(f"✅ Ордер FILLED: {order_id}")
                                fut.set_result(order_id)

                        elif exec_type == model_proto.ORDER_EXPIRED:
                            if order_id:
                                self._order_protection.pop(order_id, None)
                                self._unregister_broker_order(order_id)
                            if fut and not fut.done():
                                logging.warning(f"⏰ Ордер истёк (EXPIRED): {order_id}")
                                fut.set_result(None)

                        elif exec_type in (model_proto.ORDER_REJECTED, model_proto.ORDER_CANCELLED):
                            if order_id:
                                self._order_protection.pop(order_id, None)
                                self._unregister_broker_order(order_id)
                            if broker_order_id:
                                cfut = self._cancel_futures.pop(int(broker_order_id), None)
                                if cfut and not cfut.done():
                                    cfut.set_result(True)
                            if fut and not fut.done():
                                err_code = res.errorCode if res.HasField("errorCode") else "—"
                                label = "отменён" if exec_type == model_proto.ORDER_CANCELLED else "отклонён"
                                logging.warning(f"❌ Ордер {label} ({exec_type}): {err_code}")
                                fut.set_result(None)

                        else:
                            logging.debug(
                                f"📋 Execution {_execution_type_label(exec_type)} | order={order_id}"
                            )

                        if position_id and exec_type in (
                            model_proto.ORDER_REPLACED,
                            model_proto.ORDER_PARTIAL_FILL,
                        ):
                            self._complete_position_op(position_id)
                        elif (
                            position_id
                            and exec_type == model_proto.ORDER_FILLED
                            and res.HasField("deal")
                            and res.deal.HasField("closePositionDetail")
                        ):
                            self._complete_position_op(position_id)

                        if self._execution_callback:
                            is_position_close = False
                            deal_profit = None
                            close_position_id = None
                            if res.HasField("deal"):
                                deal = res.deal
                                sym_id = getattr(deal, "symbolId", None)
                                if sym_id:
                                    pair_name = pair_name or self._pair_for_symbol_id(sym_id)
                                if deal.HasField("closePositionDetail"):
                                    is_position_close = True
                                    close_position_id = getattr(deal, "positionId", None)
                                    deal_profit = _close_detail_net_pnl(
                                        deal.closePositionDetail, self._money_digits,
                                    )

                            cb_position_id = (
                                int(close_position_id) if close_position_id else (
                                    int(position_id) if (
                                        exec_type == model_proto.ORDER_FILLED
                                        and res.HasField("position")
                                        and position_id
                                    ) else None
                                )
                            )

                            asyncio.create_task(
                                self._execution_callback(
                                    exec_type, order_id, pair_name, broker_order_id,
                                    cb_position_id,
                                    is_position_close,
                                    deal_profit,
                                )
                            )

                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки EXECUTION_EVENT: {e}", exc_info=True)

                elif pt == _PT_ERROR_RES:
                    try:
                        err = proto.ProtoOAErrorRes()
                        err.ParseFromString(msg.payload)
                        desc = err.description if err.HasField("description") else ""
                        code = str(err.errorCode) if err.HasField("errorCode") else ""
                        if _is_rate_limit_message(desc, code):
                            await self._on_rate_limited(desc or code)
                        else:
                            logging.error(
                                f"❌ API Ошибка: {desc} (Code: {code})"
                            )
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки ERROR_RES: {e}", exc_info=True)

                elif pt == model_proto.PROTO_OA_ORDER_ERROR_EVENT:
                    try:
                        res = proto.ProtoOAOrderErrorEvent()
                        res.ParseFromString(msg.payload)
                        err_code = res.errorCode if res.HasField("errorCode") else "—"
                        desc = res.description if res.HasField("description") else str(res)
                        order_id = self._client_id_from_order_error(res)
                        logging.error(
                            f"❌ Order Error: {err_code} | {desc} | order={order_id or '—'}"
                        )
                        if order_id:
                            self._order_protection.pop(order_id, None)
                            fut = self._order_futures.pop(order_id, None)
                            if fut and not fut.done():
                                fut.set_result(None)
                        elif len(self._order_futures) == 1:
                            oid, fut = self._order_futures.popitem()
                            self._order_protection.pop(oid, None)
                            if fut and not fut.done():
                                logging.error(
                                    f"❌ Order Error (единственный pending): {oid}"
                                )
                                fut.set_result(None)
                    except Exception as e:
                        logging.error(
                            f"❌ Ошибка обработки ORDER_ERROR_EVENT: {e}",
                            exc_info=True,
                        )

                elif pt == _PT_SUBSCRIBE_SPOTS_RES:
                    pass  # подтверждение подписки, котировки придут в SPOT_EVENT

                elif pt == _PT_SPOT_EVENT:
                    try:
                        res = proto.ProtoOASpotEvent()
                        res.ParseFromString(msg.payload)

                        pair = next(
                            (p for p, info in self._pairs.items() if info[0] == res.symbolId),
                            None,
                        )
                        if pair and self._market_cache:
                            _, _, digits, pip_value, _, _, _ = self._pairs[pair]
                            bid = (
                                price_from_relative(res.bid, digits)
                                if res.HasField("bid") and res.bid > 0
                                else None
                            )
                            ask = (
                                price_from_relative(res.ask, digits)
                                if res.HasField("ask") and res.ask > 0
                                else None
                            )
                            await self._market_cache.update_quote(
                                pair=pair,
                                bid=bid,
                                ask=ask,
                                pip_value=pip_value,
                            )
                    except Exception as e:
                        logging.error(f"❌ Ошибка обработки SPOT_EVENT: {e}", exc_info=True)

                else:
                    logging.debug(f"📨 Неизвестный пакет pt={pt}, пропускаем")

        except asyncio.CancelledError:
            if not self._closing:
                raise
        except asyncio.IncompleteReadError:
            if self._closing:
                logging.debug("🔌 Соединение закрыто (disconnect)")
            else:
                logging.error("🔌 Соединение разорвано сервером (IncompleteReadError)")
                raise
        except Exception as e:
            if self._closing:
                logging.debug(f"🔌 listen_loop завершён при disconnect: {e}")
            else:
                logging.error(f"⚠️ Критическая ошибка в listen_loop: {e}")
                raise

    async def _heartbeat_loop(self):
        while True:
            await asyncio.sleep(20)
            try:
                await self._send(_PT_HEARTBEAT, common_proto.ProtoHeartbeatEvent())
            except Exception as e:
                logging.error(f"⚠️ Heartbeat остановлен: {e}")
                break