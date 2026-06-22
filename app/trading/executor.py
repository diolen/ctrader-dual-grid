"""
app/trading/executor.py
Отправка торговых ордеров через cTrader Open API.
"""

import asyncio
import logging
from typing import Optional

from app.config.settings import config
from app.models.signal import Signal as UnifiedSignal
from app.trading.position_sizer import calculate_lot

MAX_RETRIES = 3


class TradeExecutor:
    """
    Отправляет stop (v3) или limit (v2) ордера с SL через cTrader API.
    v3: trailing SL при TRAILING_STOP_LOSS=true, без TP.
    Использует клиент переданный при создании.

    Режим MANUAL/AUTO контролируется в main.py (config.TRADING_MODE).
    Executor сам режим не проверяет.

    Spread filter — только в main.py; здесь используется signal.spread_at_entry.
    """

    def __init__(self, client, market_cache=None):
        self.client = client
        self.market_cache = market_cache  # оставлен для совместимости API/тестов

    async def execute(
        self, signal: UnifiedSignal, pair: str,
    ) -> Optional[str]:
        """
        Размещает stop (v3) или limit (v2) ордер по сигналу.
        Возвращает clientOrderId при принятии брокером, None при ошибке.
        """
        info = self.client.get_pair_info(pair)
        if not info:
            logging.error(f"❌ [{pair}] Пара не инициализирована")
            return None

        symbol_id, multiplier, digits, pip_value, min_volume, step_volume = info

        balance = await self.client.get_balance()
        if balance is None:
            logging.error(f"❌ [{pair}] Не удалось получить баланс счёта")
            return None

        risk_pct = config.get_pair_config(pair).risk_pct

        lot = calculate_lot(
            balance    = balance,
            risk_pct   = risk_pct,
            entry      = signal.entry,
            stop_loss  = signal.stop_loss,
            pip_value  = pip_value,
            max_lot    = config.MAX_LOT,
            min_volume = min_volume,
        )

        if lot <= 0:
            logging.error(f"❌ [{pair}] Некорректный лот ({lot}) — ордер отменён")
            return None

        entry       = round(signal.entry,      digits)
        stop_loss   = round(signal.stop_loss,  digits)
        use_stop = signal.strategy_type == "BREAKOUT_RETEST_V3"
        place_order = (
            self.client.place_stop_order if use_stop else self.client.place_limit_order
        )
        order_kind = "Stop" if use_stop else "Limit"
        order_id = None
        resolved_volume = None
        for attempt in range(1, MAX_RETRIES + 1):
            if attempt > 1:
                logging.info(
                    f"📤 [{pair}] Повтор {attempt}/{MAX_RETRIES} | "
                    f"lot={lot:.2f} entry={entry}"
                )

            placement = await place_order(
                symbol_id   = symbol_id,
                direction   = signal.direction,
                lot         = lot,
                entry       = entry,
                stop_loss   = stop_loss,
                multiplier  = multiplier,
                min_volume  = min_volume,
                step_volume = step_volume,
                pair        = pair,
            )
            if placement:
                order_id, resolved_volume = placement
                break
            logging.warning(f"⚠️ [{pair}] Попытка {attempt}/{MAX_RETRIES} не удалась")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2 * attempt)

        if order_id:
            spread = getattr(signal, "spread_at_entry", None)
            spread_str = f"{spread:.1f}p" if spread is not None else "N/A"
            if resolved_volume:
                lot_part = (
                    f"lot={resolved_volume.actual_lot:.2f} "
                    f"({resolved_volume.actual_volume_cents} cents)"
                )
            else:
                lot_part = f"lot={lot:.2f}"
            trail = "on" if config.TRAILING_STOP_LOSS else "off"
            logging.info(
                f"✅ [{pair}] {order_kind} ACCEPTED | {signal.direction} | {lot_part} | "
                f"entry={entry} sl={stop_loss} | trailing_sl={trail} | "
                f"spread={spread_str} | order={order_id}"
            )
            return order_id

        logging.error(
            f"❌ [{pair}] Ошибка размещения ордера после {MAX_RETRIES} попыток | "
            f"{signal.direction} entry={signal.entry} sl={signal.stop_loss} tp={signal.take_profit}"
        )
        return None

