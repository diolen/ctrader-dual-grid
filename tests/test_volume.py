from app.trading.volume import (
    PRICE_SCALE,
    VOLUME_CENTS_PER_LOT,
    lot_to_volume_cents,
    pip_value_from_symbol,
    price_from_relative,
    resolve_order_volume,
    volume_cents_to_lot,
)


def test_price_from_relative_usdjpy():
    # raw ~159765000 → 1597.65 без округления; типичный raw 15976500 → 159.765
    assert price_from_relative(15_976_500, 3) == round(15_976_500 / PRICE_SCALE, 3)
    assert price_from_relative(159_765_00, 3) == 159.765


def test_pip_value_from_symbol():
    assert pip_value_from_symbol(5, 4) == 0.0001
    assert pip_value_from_symbol(5, 5) == 0.0001
    assert pip_value_from_symbol(3, 2) == 0.01
    assert pip_value_from_symbol(3, 3) == 0.01
    # JPY-пары: digits=3, пип = 0.01, не 0.0001
    assert pip_value_from_symbol(5, 4, pair="EURJPY") == 0.01
    assert pip_value_from_symbol(5, 3, pair="EURJPY") == 0.01


def test_lot_to_volume_cents():
    assert lot_to_volume_cents(0.39) == 3_900_000
    assert lot_to_volume_cents(0.01) == 100_000
    assert volume_cents_to_lot(100_000) == 0.01


def test_resolve_bump_to_min():
    vol = resolve_order_volume(lot=0.39, min_volume_cents=100_000)
    assert vol.calculated_volume_cents == 3_900_000
    assert vol.actual_volume_cents == 3_900_000
    assert not vol.bumped_to_min


def test_resolve_bump_when_below_min():
    vol = resolve_order_volume(lot=0.005, min_volume_cents=100_000)
    assert vol.calculated_volume_cents == 50_000
    assert vol.actual_volume_cents == 100_000
    assert vol.bumped_to_min


def test_relative_stop_loss_scale():
    from app.trading.volume import PRICE_SCALE, relative_stop_loss_distance

    rel_sl = relative_stop_loss_distance(1.34740, 1.34716)
    assert rel_sl == int(round(0.00024 * PRICE_SCALE))


def test_relative_sltp_scale():
    from app.trading.volume import PRICE_SCALE, relative_sltp_distance
    rel_sl, rel_tp = relative_sltp_distance(1.34740, 1.34716, 1.34756)
    assert rel_sl == round(0.00024 * PRICE_SCALE)
    assert rel_tp == round(0.00016 * PRICE_SCALE)


def test_resolve_step_rounding():
    vol = resolve_order_volume(lot=0.01, min_volume_cents=100_000, step_volume_cents=100_000)
    assert vol.actual_volume_cents == 100_000
    assert VOLUME_CENTS_PER_LOT == 10_000_000
