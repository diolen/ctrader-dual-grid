"""Tests for per-pair GridBook."""

from app.trading.grid_book import GridBook, PairGrids
from app.trading.grid_manager import GridManager
from app.trading.grid_models import GridPosition, Direction


def test_grid_book_creates_pair_grids():
    book = GridBook(["EURUSD", "BTCUSD"])
    assert book.pairs() == ["EURUSD", "BTCUSD"]
    eur = book.get("EURUSD")
    assert eur.long_grid.direction == Direction.LONG
    assert eur.short_grid.direction == Direction.SHORT


def test_pair_grids_exposure():
    pg = PairGrids(
        pair="EURUSD",
        long_grid=GridManager(Direction.LONG),
        short_grid=GridManager(Direction.SHORT),
    )
    pg.long_grid.add_position(GridPosition(
        direction=Direction.LONG,
        entry_price=1.1,
        volume=0.02,
        stop_loss=1.09,
        client_order_id="l1",
    ))
    pg.short_grid.add_position(GridPosition(
        direction=Direction.SHORT,
        entry_price=1.1,
        volume=0.01,
        stop_loss=1.11,
        client_order_id="s1",
    ))
    assert pg.pair_long_lots() == 0.02
    assert pg.pair_short_lots() == 0.01
    assert pg.pair_exposure_lots() == 0.03


def test_find_position_by_client_order_id():
    book = GridBook(["EURUSD"])
    pg = book.get("EURUSD")
    pg.long_grid.add_position(GridPosition(
        direction=Direction.LONG,
        entry_price=1.1,
        volume=0.01,
        stop_loss=1.09,
        client_order_id="ord-x",
        pair="EURUSD",
    ))
    found = book.find_position_by_client_order_id("ord-x")
    assert found is not None
    found_pg, pos = found
    assert found_pg.pair == "EURUSD"
    assert pos.client_order_id == "ord-x"


def test_estimated_total_margin():
    book = GridBook(["EURUSD", "BTCUSD"])
    book.get("EURUSD").long_grid.add_position(GridPosition(
        direction=Direction.LONG,
        entry_price=1.1,
        volume=0.10,
        stop_loss=1.09,
        client_order_id="l1",
    ))
    book.get("BTCUSD").short_grid.add_position(GridPosition(
        direction=Direction.SHORT,
        entry_price=50000,
        volume=0.01,
        stop_loss=51000,
        client_order_id="s1",
    ))
    margin = book.estimated_total_margin({"EURUSD": 1000.0, "BTCUSD": 2000.0})
    assert margin == 0.10 * 1000.0 + 0.01 * 2000.0
