"""
app/trading/position_sizer.py
Расчёт размера позиции на основе % риска от депозита.
"""

import math
import logging

from app.trading.volume import VOLUME_CENTS_PER_LOT, volume_cents_to_lot


def calculate_lot(
    balance:    float,
    risk_pct:   float,
    entry:      float,
    stop_loss:  float,
    pip_value:  float,
    lot_size:   float = 100_000,  # стандартный лот для forex
    min_lot:    float = 0.01,
    max_lot:    float = 1.0,
    lot_step:   float = 0.01,
    min_volume: int   = 0,        # минимальный объём брокера в единицах (0 = не проверять)
) -> float:
    """
    Рассчитывает размер лота по формуле:

        risk_amount   = balance * risk_pct / 100
        sl_pips       = abs(entry - stop_loss) / pip_value
        pip_value_lot = pip_value * lot_size       (стоимость 1 пипса на 1 лот)
        lot           = risk_amount / (sl_pips * pip_value_lot)

    Упрощается до:
        lot = risk_amount / (abs(entry - stop_loss) * lot_size)

    Примечание: формула корректна для пар где котируемая валюта = валюта депозита
    (EURUSD, GBPUSD с USD-депозитом). Для кросс-пар нужен курс конвертации.

    Параметры:
        balance    — баланс счёта в валюте депозита
        risk_pct   — риск на сделку в % (например 1.0 = 1%)
        entry      — цена входа
        stop_loss  — цена стоп-лосса
        pip_value  — размер пипса (0.0001 для EURUSD/GBPUSD)
        lot_size   — размер стандартного лота (100 000 единиц)
        min_lot    — минимальный лот (0.01)
        max_lot    — максимальный лот (из config.MAX_LOT)
        lot_step   — шаг лота (0.01)
        min_volume — минимальный объём брокера в центах API (100000 = 0.01 lot)
                     если расчётный объём меньше — только предупреждение в лог

    Возвращает:
        Размер лота, округлённый вниз до lot_step.
        0.0 при некорректных входных данных — executor должен отменить ордер.
    """

    # ── Валидация входных данных ─────────────────────────────────
    if balance <= 0:
        logging.error(f"❌ Position sizer: некорректный баланс: {balance}")
        return 0.0

    if risk_pct <= 0:
        logging.error(f"❌ Position sizer: некорректный риск: {risk_pct}%")
        return 0.0

    sl_distance = abs(entry - stop_loss)
    if sl_distance < 1e-8:
        logging.error(f"❌ Position sizer: entry == stop_loss ({entry}), ордер отменён")
        return 0.0

    sl_pips = sl_distance / pip_value
    if sl_pips < 1.0:
        logging.error(f"❌ Position sizer: SL слишком мал ({sl_pips:.2f} pips) — возможна ошибка в сигнале")
        return 0.0

    # ── Расчёт лота ──────────────────────────────────────────────
    risk_amount   = balance * risk_pct / 100
    pip_value_lot = pip_value * lot_size        # стоимость 1 пипса на 1 лот
    lot           = risk_amount / (sl_pips * pip_value_lot)

    # Округляем вниз до lot_step — не берём больше риска чем планировали
    lot = math.floor(lot / lot_step) * lot_step
    lot = round(lot, 2)

    # ── Ограничения ──────────────────────────────────────────────
    if lot < min_lot:
        logging.warning(
            f"⚠️ Position sizer: расчётный лот {lot:.2f} < min_lot {min_lot} "
            f"(баланс {balance:.2f}, риск {risk_pct}%, SL {sl_pips:.1f} pips) — используем min_lot"
        )
        lot = min_lot

    if lot > max_lot:
        logging.warning(
            f"⚠️ Position sizer: расчётный лот {lot:.2f} > max_lot {max_lot} — обрезаем"
        )
        lot = max_lot

    # ── Проверка минимального объёма брокера (cents API) ─────────
    below_min_volume = False
    calculated_volume_cents = int(round(lot * VOLUME_CENTS_PER_LOT))
    min_lot_broker = volume_cents_to_lot(min_volume) if min_volume > 0 else 0.0

    if min_volume > 0 and calculated_volume_cents < min_volume:
        below_min_volume = True
        balance_for_min = min_lot_broker / (risk_pct / 100) * sl_pips * pip_value_lot
        calc_lot = volume_cents_to_lot(calculated_volume_cents)
        logging.warning(
            f"⚠️ Position sizer: расчёт {calc_lot:.2f} lot ({calculated_volume_cents} cents) "
            f"< min брокера {min_lot_broker:.2f} lot ({min_volume} cents). "
            f"Для риска {risk_pct}% при SL {sl_pips:.1f}p нужен баланс ~{balance_for_min:.0f}. "
            f"Ордер уйдёт с min_volume."
        )

    info = (
        f"📐 Position sizer: balance={balance:.2f} risk={risk_pct}% "
        f"SL={sl_pips:.1f}pips pip_val={pip_value} → lot={lot:.2f}"
    )
    if below_min_volume:
        info += (
            f" | ⚠️ vol<min: расчёт {volume_cents_to_lot(calculated_volume_cents):.2f} lot "
            f"→ факт min {min_lot_broker:.2f} lot"
        )
    logging.info(info)
    return lot