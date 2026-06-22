"""
Конвертация лотов ↔ объём cTrader Open API.

В API объём задаётся в центах: 10_000_000 cents = 1 стандартный лот.
Расчёт риска (pip value) использует классический forex lot = 100_000 единиц базовой валюты.
"""

from dataclasses import dataclass

# cTrader Open API: volume / lotSize в центах
VOLUME_CENTS_PER_LOT = 10_000_000
# cTrader Open API: trendbar/spot цены в relative-формате, всегда / 100_000
PRICE_SCALE = 100_000


def price_from_relative(relative: int, digits: int) -> float:
    """Relative → цена символа (см. cTrader Open API symbol data)."""
    return round(int(relative) / PRICE_SCALE, digits)
# Классический размер лота для расчёта стоимости пипса ($10/пип на 1 лот EURUSD)
PIP_LOT_UNITS = 100_000


def relative_stop_loss_distance(entry: float, stop_loss: float) -> int:
    """relativeStopLoss для ProtoOANewOrderReq (только SL на лимитке)."""
    sl_dist = abs(entry - stop_loss)
    rel_sl = int(round(sl_dist * PRICE_SCALE))
    if rel_sl <= 0:
        raise ValueError(f"invalid SL distance sl={sl_dist}")
    return rel_sl


def partial_close_volume_cents(
    total_cents: int,
    tp1_size_percent: float,
    min_volume_cents: int,
    step_volume_cents: int,
) -> int:
    """Объём частичного закрытия на TP1 с учётом min/step брокера."""
    if total_cents <= 0:
        return 0
    frac = tp1_size_percent / 100.0
    close_cents = int(round(total_cents * frac))
    if step_volume_cents > 0:
        stepped = (close_cents // step_volume_cents) * step_volume_cents
        if stepped < min_volume_cents:
            stepped = min_volume_cents
        close_cents = stepped
    if close_cents < min_volume_cents:
        close_cents = min_volume_cents
    remainder = total_cents - close_cents
    if remainder < min_volume_cents:
        close_cents = total_cents - min_volume_cents
        if step_volume_cents > 0:
            close_cents = (close_cents // step_volume_cents) * step_volume_cents
        if close_cents < min_volume_cents:
            return total_cents
    return max(min_volume_cents, min(close_cents, total_cents - min_volume_cents))


def relative_take_profit_distance(entry: float, take_profit: float) -> int:
    """relativeTakeProfit для ProtoOANewOrderReq."""
    tp_dist = abs(take_profit - entry)
    rel_tp = int(round(tp_dist * PRICE_SCALE))
    if rel_tp <= 0:
        raise ValueError(f"invalid TP distance tp={tp_dist}")
    return rel_tp


def relative_sltp_distance(
    entry: float,
    stop_loss: float,
    take_profit: float,
) -> tuple[int, int]:
    """Для бэктеста / расчётов с TP."""
    sl_dist = abs(entry - stop_loss)
    tp_dist = abs(take_profit - entry)
    rel_sl = int(round(sl_dist * PRICE_SCALE))
    rel_tp = int(round(tp_dist * PRICE_SCALE))
    if rel_sl <= 0 or rel_tp <= 0:
        raise ValueError(f"invalid SL/TP distance sl={sl_dist} tp={tp_dist}")
    return rel_sl, rel_tp


def pip_value_from_symbol(
    digits: int,
    pip_position: int,
    pair: str = "",
) -> float:
    """
    Размер пипса в единицах цены котировки.
    JPY-пары: 0.01 (котировка ~159.765 при digits=3).
    Остальные: 10^(pip_position - digits), иначе digits<=3 → 0.01, иначе 0.0001.
    """
    _ = pip_position
    p = pair.upper()
    if p.endswith("JPY") or (len(p) == 6 and "JPY" in p):
        return 0.01
    if digits <= 3:
        return 0.01
    return 0.0001


def lot_to_volume_cents(lot: float) -> int:
    return int(round(lot * VOLUME_CENTS_PER_LOT))


def volume_cents_to_lot(volume_cents: int) -> float:
    return volume_cents / VOLUME_CENTS_PER_LOT


def round_volume_to_step(volume_cents: int, step_volume_cents: int) -> int:
    if step_volume_cents <= 0:
        return volume_cents
    if volume_cents % step_volume_cents == 0:
        return volume_cents
    return ((volume_cents // step_volume_cents) + 1) * step_volume_cents


@dataclass(frozen=True)
class ResolvedVolume:
    calculated_lot: float
    calculated_volume_cents: int
    actual_volume_cents: int
    min_volume_cents: int
    step_volume_cents: int
    bumped_to_min: bool
    rounded_to_step: bool

    @property
    def actual_lot(self) -> float:
        return volume_cents_to_lot(self.actual_volume_cents)


def resolve_order_volume(
    lot: float,
    min_volume_cents: int,
    step_volume_cents: int = 0,
) -> ResolvedVolume:
    """Рассчитанный объём с подъёмом до min и округлением по step (вверх)."""
    calculated_cents = lot_to_volume_cents(lot)
    actual = calculated_cents
    bumped = False
    if min_volume_cents > 0 and actual < min_volume_cents:
        actual = min_volume_cents
        bumped = True
    rounded = False
    if step_volume_cents > 0:
        stepped = round_volume_to_step(actual, step_volume_cents)
        rounded = stepped != actual
        actual = stepped
    return ResolvedVolume(
        calculated_lot=lot,
        calculated_volume_cents=calculated_cents,
        actual_volume_cents=actual,
        min_volume_cents=min_volume_cents,
        step_volume_cents=step_volume_cents,
        bumped_to_min=bumped,
        rounded_to_step=rounded,
    )
