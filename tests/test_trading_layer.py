"""Tests for Trading Layer - Dual Grid Strategy."""

import pytest
import pandas as pd
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, AsyncMock, MagicMock

from app.trading.grid_models import GridPosition, Direction, ExecutionApproval, DegradationMode
from app.trading.grid_manager import GridManager
from app.trading.grid_book import GridBook, PairGrids
from app.trading.portfolio_manager import PortfolioManager
from app.trading.atr_calculator import compute_atr, compute_atr_baseline
from app.trading.trading_engine import TradingEngine, RollingWindow, _broker_direction_to_grid, _is_watched


def _make_engine(*pairs: str) -> TradingEngine:
    """TradingEngine with explicit watched pairs (isolated from .env)."""
    from app.config.settings import config
    object.__setattr__(config, "WATCHED_INSTRUMENTS", [(p, "M5") for p in pairs])
    return TradingEngine()


class TestGridModels:
    """Test data models."""
    
    def test_grid_position_creation(self):
        """Test GridPosition creation."""
        position = GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=0.1,
            stop_loss=1.0900,
            client_order_id="test_order_123",
        )
        assert position.direction == Direction.LONG
        assert position.entry_price == 1.1000
        assert position.volume == 0.1
        assert position.stop_loss == 1.0900
        assert position.client_order_id == "test_order_123"
        assert position.position_id is None
        assert position.level_index == 0
    
    def test_execution_approval(self):
        """Test ExecutionApproval."""
        approval = ExecutionApproval(approved=True, volume_multiplier=0.5)
        assert approval.approved is True
        assert approval.volume_multiplier == 0.5


class TestGridManager:
    """Test GridManager."""
    
    def test_grid_manager_creation(self):
        """Test GridManager creation."""
        manager = GridManager(Direction.LONG)
        assert manager.direction == Direction.LONG
        assert manager.is_active() is False
        assert manager.level_count() == 0
    
    def test_add_position(self):
        """Test adding position to grid."""
        manager = GridManager(Direction.LONG)
        position = GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=0.1,
            stop_loss=1.0900,
            client_order_id="test_123",
            level_index=1,
        )
        manager.add_position(position)
        assert manager.is_active() is True
        assert manager.level_count() == 1
        assert manager.get_last_position() == position
    
    def test_total_exposure(self):
        """Test total exposure calculation."""
        manager = GridManager(Direction.LONG)
        manager.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=0.1,
            stop_loss=1.0900,
            client_order_id="test_1",
        ))
        manager.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1050,
            volume=0.2,
            stop_loss=1.0950,
            client_order_id="test_2",
        ))
        assert manager.total_exposure_lots() == pytest.approx(0.3)
    
    def test_remove_position(self):
        """Test removing position."""
        manager = GridManager(Direction.LONG)
        position = GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=0.1,
            stop_loss=1.0900,
            client_order_id="test_123",
            position_id="pos_123",
        )
        manager.add_position(position)
        removed = manager.remove_position("pos_123")
        assert removed == position
        assert manager.level_count() == 0
    
    def test_can_add_level(self):
        """Test can_add_level with max_levels."""
        manager = GridManager(Direction.LONG)
        assert manager.can_add_level(max_levels=5) is True
        
        for i in range(5):
            manager.add_position(GridPosition(
                direction=Direction.LONG,
                entry_price=1.1000 + i * 0.0010,
                volume=0.1,
                stop_loss=1.0900,
                client_order_id=f"test_{i}",
            ))
        
        assert manager.can_add_level(max_levels=5) is False
    
    def test_long_short_independence(self):
        """Test independence of Long and Short grids."""
        long_manager = GridManager(Direction.LONG)
        short_manager = GridManager(Direction.SHORT)
        
        long_manager.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=0.1,
            stop_loss=1.0900,
            client_order_id="long_1",
        ))
        
        assert long_manager.is_active() is True
        assert short_manager.is_active() is False
        
        short_manager.add_position(GridPosition(
            direction=Direction.SHORT,
            entry_price=1.1000,
            volume=0.1,
            stop_loss=1.1100,
            client_order_id="short_1",
        ))
        
        assert long_manager.level_count() == 1
        assert short_manager.level_count() == 1


class TestPortfolioManager:
    """Test PortfolioManager."""
    
    def test_initialize_baseline(self):
        """Test baseline equity initialization."""
        manager = PortfolioManager()
        assert manager.baseline_equity == 0.0
        
        manager.initialize_baseline(10000.0)
        assert manager.baseline_equity == 10000.0
        
        # Should not reinitialize
        manager.initialize_baseline(15000.0)
        assert manager.baseline_equity == 10000.0
    
    def test_restart(self):
        """Test restart after halt."""
        manager = PortfolioManager()
        manager.initialize_baseline(10000.0)
        manager.consecutive_execution_rejections = 5
        manager.halted = True
        
        manager.restart(12000.0)
        assert manager.baseline_equity == 12000.0
        assert manager.consecutive_execution_rejections == 0
        assert manager.halted is False
    
    def test_calculate_portfolio_pnl_fraction(self):
        """Test portfolio PnL fraction calculation."""
        manager = PortfolioManager()
        manager.initialize_baseline(10000.0)
        
        pnl = manager.calculate_portfolio_pnl_fraction(500.0)
        assert pnl == 0.05
        
        pnl = manager.calculate_portfolio_pnl_fraction(-500.0)
        assert pnl == -0.05
    
    def test_calculate_portfolio_pnl_zero_baseline(self):
        """Test PnL calculation with zero baseline (should halt)."""
        manager = PortfolioManager()
        # Don't initialize baseline
        
        manager.calculate_portfolio_pnl_fraction(500.0)
        assert manager.halted is True
    
    def test_can_expand_exposure_limit(self):
        """Test can_expand with per-pair exposure limit."""
        manager = PortfolioManager()
        manager.initialize_baseline(10000.0)

        grid_book = GridBook(["EURUSD"])
        pair_grids = grid_book.get("EURUSD")
        pair_grids.long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=10.0,
            stop_loss=1.0900,
            client_order_id="test_1",
        ))

        manager.margin_per_lot = 1000.0
        margin_by_pair = {"EURUSD": 1000.0}

        result = manager.can_expand(
            "EURUSD",
            Direction.LONG,
            pair_grids,
            grid_book,
            10000.0,
            1000.0,
            additional_lots=0.01,
            margin_by_pair=margin_by_pair,
        )
        assert result is False

    def test_can_expand_imbalance_hard_threshold(self):
        """Test can_expand with per-pair imbalance hard threshold."""
        manager = PortfolioManager()
        manager.initialize_baseline(10000.0)
        manager.margin_per_lot = 1000.0

        grid_book = GridBook(["EURUSD"])
        pair_grids = grid_book.get("EURUSD")
        pair_grids.long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=9.0,
            stop_loss=1.0900,
            client_order_id="long_1",
        ))
        pair_grids.short_grid.add_position(GridPosition(
            direction=Direction.SHORT,
            entry_price=1.1000,
            volume=1.0,
            stop_loss=1.1100,
            client_order_id="short_1",
        ))

        result = manager.can_expand(
            "EURUSD",
            Direction.LONG,
            pair_grids,
            grid_book,
            10000.0,
            1000.0,
            additional_lots=0.01,
            margin_by_pair={"EURUSD": 1000.0},
        )
        assert result is False

    def test_can_expand_cross_pair_no_imbalance_block(self):
        """Imbalance on EURUSD must not block BTCUSD LONG entry."""
        manager = PortfolioManager()
        manager.margin_per_lot = 1000.0

        grid_book = GridBook(["EURUSD", "BTCUSD"])
        eur = grid_book.get("EURUSD")
        eur.long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=0.09,
            stop_loss=1.0900,
            client_order_id="long_1",
        ))
        eur.short_grid.add_position(GridPosition(
            direction=Direction.SHORT,
            entry_price=1.1000,
            volume=0.01,
            stop_loss=1.1100,
            client_order_id="short_1",
        ))

        btc = grid_book.get("BTCUSD")
        result = manager.can_expand(
            "BTCUSD",
            Direction.LONG,
            btc,
            grid_book,
            10000.0,
            2000.0,
            additional_lots=0.01,
            margin_by_pair={"EURUSD": 1000.0, "BTCUSD": 2000.0},
        )
        assert result is True

    def test_get_add_level_threshold_adjustment(self):
        """Test score threshold adjustment based on per-pair imbalance."""
        from app.config.settings import config

        manager = PortfolioManager()
        pair_grids = PairGrids(
            pair="EURUSD",
            long_grid=GridManager(Direction.LONG),
            short_grid=GridManager(Direction.SHORT),
        )

        adjustment = manager.get_add_level_threshold_adjustment(pair_grids)
        assert adjustment == 0.0

        pair_grids.long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=8.0,
            stop_loss=1.0900,
            client_order_id="long_1",
        ))
        pair_grids.short_grid.add_position(GridPosition(
            direction=Direction.SHORT,
            entry_price=1.1000,
            volume=2.0,
            stop_loss=1.1100,
            client_order_id="short_1",
        ))

        adjustment = manager.get_add_level_threshold_adjustment(pair_grids)
        assert adjustment == config.IMBALANCE_SCORE_PENALTY
    
    def test_determine_degradation_mode_normal(self):
        """Test degradation mode - Normal."""
        manager = PortfolioManager()
        
        mode = manager.determine_degradation_mode(
            portfolio_pnl_fraction=0.0,
            current_atr=0.0010,
            atr_baseline=0.0010,
        )
        assert mode == DegradationMode.NORMAL
    
    def test_determine_degradation_mode_exit(self):
        """Test degradation mode - Exit (highest priority)."""
        manager = PortfolioManager()
        
        mode = manager.determine_degradation_mode(
            portfolio_pnl_fraction=-0.06,  # Below EXIT_MODE_DRAWDOWN_THRESHOLD
            current_atr=0.0010,
            atr_baseline=0.0010,
        )
        assert mode == DegradationMode.EXIT
    
    def test_determine_degradation_mode_freeze_atr(self):
        """Test degradation mode - Freeze (ATR spike)."""
        manager = PortfolioManager()
        
        mode = manager.determine_degradation_mode(
            portfolio_pnl_fraction=0.0,
            current_atr=0.0040,  # 4x baseline
            atr_baseline=0.0010,
        )
        assert mode == DegradationMode.FREEZE
    
    def test_determine_degradation_mode_freeze_rejections(self):
        """Test degradation mode - Freeze (consecutive rejections)."""
        manager = PortfolioManager()
        manager.consecutive_execution_rejections = 6  # Above FREEZE_CONSECUTIVE_REJECTIONS
        
        mode = manager.determine_degradation_mode(
            portfolio_pnl_fraction=0.0,
            current_atr=0.0010,
            atr_baseline=0.0010,
        )
        assert mode == DegradationMode.FREEZE
    
    def test_determine_degradation_mode_conservative_atr(self):
        """Test degradation mode - Conservative (ATR spike)."""
        manager = PortfolioManager()
        
        mode = manager.determine_degradation_mode(
            portfolio_pnl_fraction=0.0,
            current_atr=0.0025,  # 2.5x baseline
            atr_baseline=0.0010,
        )
        assert mode == DegradationMode.CONSERVATIVE
    
    def test_determine_degradation_mode_freeze_priority(self):
        """Test Freeze has priority over Conservative."""
        manager = PortfolioManager()
        manager.consecutive_execution_rejections = 6
        
        mode = manager.determine_degradation_mode(
            portfolio_pnl_fraction=0.0,
            current_atr=0.0025,  # Would trigger Conservative
            atr_baseline=0.0010,
        )
        # Freeze should win due to rejections
        assert mode == DegradationMode.FREEZE
    
    def test_check_global_tp_sl(self):
        """Test global TP/SL check."""
        manager = PortfolioManager()
        
        # Target hit
        result = manager.check_global_tp_sl(0.16)
        assert result == "TP"
        
        # Stop loss hit
        result = manager.check_global_tp_sl(-0.11)
        assert result == "SL"
        
        # Neither
        result = manager.check_global_tp_sl(0.0)
        assert result is None
    
    def test_execution_rejection_counter(self):
        """Test consecutive execution rejections counter."""
        manager = PortfolioManager()
        
        assert manager.consecutive_execution_rejections == 0
        
        manager.increment_execution_rejection()
        assert manager.consecutive_execution_rejections == 1
        
        manager.increment_execution_rejection()
        assert manager.consecutive_execution_rejections == 2
        
        manager.reset_execution_rejections()
        assert manager.consecutive_execution_rejections == 0


class TestATRCalculator:
    """Test ATR calculation."""
    
    def test_compute_atr_wilder_smoothing(self):
        """Test ATR calculation with Wilder's smoothing."""
        # Create test data with known ATR result
        data = {
            'timestamp': pd.date_range(start='2024-01-01', periods=20, freq='D'),
            'open': [1.1000 + i * 0.0001 for i in range(20)],
            'high': [1.1010 + i * 0.0001 for i in range(20)],
            'low': [1.0990 + i * 0.0001 for i in range(20)],
            'close': [1.1005 + i * 0.0001 for i in range(20)],
            'volume': [1000] * 20,
        }
        df = pd.DataFrame(data)
        
        atr = compute_atr(df, period=14)
        assert atr > 0
        assert isinstance(atr, float)
    
    def test_compute_atr_insufficient_data(self):
        """Test ATR with insufficient data."""
        data = {
            'timestamp': pd.date_range(start='2024-01-01', periods=10, freq='D'),
            'open': [1.1000 + i * 0.0001 for i in range(10)],
            'high': [1.1010 + i * 0.0001 for i in range(10)],
            'low': [1.0990 + i * 0.0001 for i in range(10)],
            'close': [1.1005 + i * 0.0001 for i in range(10)],
            'volume': [1000] * 10,
        }
        df = pd.DataFrame(data)
        
        atr = compute_atr(df, period=14)
        assert atr == 0.0
    
    def test_compute_atr_baseline(self):
        """Test ATR baseline calculation."""
        atr_values = [0.0010, 0.0012, 0.0011, 0.0013, 0.0010]
        baseline = compute_atr_baseline(atr_values)
        
        expected = sum(atr_values) / len(atr_values)
        assert baseline == expected
    
    def test_compute_atr_baseline_empty(self):
        """Test ATR baseline with empty history."""
        baseline = compute_atr_baseline([])
        assert baseline == 0.0


class TestRollingWindow:
    """Test RollingWindow utility."""
    
    def test_rolling_window_add(self):
        """Test adding values to rolling window."""
        window = RollingWindow(max_size=5)
        
        window.add(1.0)
        window.add(2.0)
        window.add(3.0)
        
        assert window.mean() == 2.0
        assert len(window.window) == 3
    
    def test_rolling_window_max_size(self):
        """Test rolling window respects max size."""
        window = RollingWindow(max_size=3)
        
        window.add(1.0)
        window.add(2.0)
        window.add(3.0)
        window.add(4.0)
        
        assert len(window.window) == 3
        assert window.mean() == 3.0  # (2.0 + 3.0 + 4.0) / 3
    
    def test_rolling_window_is_full(self):
        """Test is_full check."""
        window = RollingWindow(max_size=3)
        
        assert window.is_full() is False
        
        window.add(1.0)
        window.add(2.0)
        window.add(3.0)
        
        assert window.is_full() is True


class TestTradingEngine:
    """Test TradingEngine."""
    
    def test_trading_engine_initialization(self):
        """Test TradingEngine initialization."""
        engine = _make_engine("EURUSD")
        eur = engine.grids.get("EURUSD")

        assert eur.long_grid.direction == Direction.LONG
        assert eur.short_grid.direction == Direction.SHORT
        assert engine.portfolio.baseline_equity == 0.0
        assert len(engine.pending_order_metadata) == 0
    
    def test_restart(self):
        """Test TradingEngine restart."""
        engine = _make_engine("EURUSD")
        engine.portfolio.initialize_baseline(10000.0)
        engine.portfolio.consecutive_execution_rejections = 5
        engine.portfolio.halted = True
        engine.pending_order_metadata["test"] = {"data": "test"}
        
        engine.restart(12000.0)
        
        assert engine.portfolio.baseline_equity == 12000.0
        assert engine.portfolio.consecutive_execution_rejections == 0
        assert engine.portfolio.halted is False
        assert len(engine.pending_order_metadata) == 0
    
    def test_candles_to_dataframe(self):
        """Test candle to DataFrame conversion."""
        from app.models.candle import Candle
        
        engine = _make_engine("EURUSD")
        
        candles = [
            Candle(
                timestamp=datetime(2024, 1, 1),
                open=1.1000,
                high=1.1010,
                low=1.0990,
                close=1.1005,
                volume=1000,
            ),
            Candle(
                timestamp=datetime(2024, 1, 2),
                open=1.1005,
                high=1.1015,
                low=1.0995,
                close=1.1010,
                volume=1100,
            ),
        ]
        
        df = engine._candles_to_dataframe(candles)
        
        assert len(df) == 2
        assert list(df.columns) == ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        assert df['close'].iloc[0] == 1.1005
        assert df['close'].iloc[1] == 1.1010
    
    async def test_execution_check_cold_start(self):
        """Test execution check passes on cold start (window not full)."""
        engine = _make_engine("EURUSD")

        client = Mock()
        client.get_spread = AsyncMock(return_value=1.5)

        approval = await engine._execution_check("EURUSD", client, current_atr=0.0010)

        assert approval.approved is True
        assert approval.volume_multiplier == 1.0

    async def test_execution_check_spread_rejection(self):
        """Test execution check rejects on high spread."""
        engine = _make_engine("EURUSD")

        for _ in range(20):
            engine.spread_history["EURUSD"].add(1.0)

        for _ in range(50):
            engine.atr_baseline_history["EURUSD"].add(0.0010)

        client = Mock()
        client.get_spread = AsyncMock(return_value=3.0)

        approval = await engine._execution_check("EURUSD", client, current_atr=0.0010)

        assert approval.approved is False

    async def test_execution_check_warn_zone(self):
        """Test execution check reduces volume in warn zone."""
        from app.config.settings import config

        engine = _make_engine("EURUSD")

        for _ in range(20):
            engine.spread_history["EURUSD"].add(1.0)

        for _ in range(50):
            engine.atr_baseline_history["EURUSD"].add(0.0010)

        client = Mock()
        client.get_spread = AsyncMock(return_value=1.8)

        approval = await engine._execution_check("EURUSD", client, current_atr=0.0010)

        assert approval.approved is True
        assert approval.volume_multiplier == config.EXECUTION_WARN_VOLUME_REDUCTION


class TestTradingEngineHelpers:
    """Helper and integration-style unit tests."""

    def test_broker_direction_mapping(self):
        assert _broker_direction_to_grid("BUY") == Direction.LONG
        assert _broker_direction_to_grid("SELL") == Direction.SHORT

    def test_is_watched_pair(self):
        watched = [("EURUSD", "M5"), ("GBPUSD", "M15")]
        assert _is_watched("EURUSD", "M5", watched) is True
        assert _is_watched("EURUSD", "M15", watched) is False
        assert _is_watched("XAUUSD", "M5", watched) is False

    async def test_on_bar_update_skips_unwatched_pair(self):
        engine = _make_engine("EURUSD")
        client = AsyncMock()
        orchestrator = AsyncMock()
        state = Mock(pair="XAUUSD", entry_tf="M5", symbol_id=2, candles=[], entry_minutes=5)

        await engine.on_bar_update(state, orchestrator, client, None)

        client.get_positions.assert_not_called()
        client.get_spread.assert_not_called()

    async def test_on_bar_update_halted_skips_trading(self):
        from app.config.settings import config
        from app.models.candle import Candle

        engine = _make_engine("EURUSD")
        engine.portfolio.halted = True
        engine.spread_history["EURUSD"] = RollingWindow(config.SPREAD_LOOKBACK_BARS)
        engine.atr_baseline_history["EURUSD"] = RollingWindow(config.ATR_BASELINE_LOOKBACK_BARS)

        candles = [
            Candle(
                timestamp=datetime(2024, 1, 1),
                open=1.1, high=1.11, low=1.09, close=1.105, volume=100,
            ),
        ]
        state = Mock(
            pair="EURUSD", entry_tf="M5", symbol_id=1,
            candles=candles, entry_minutes=5,
        )
        client = AsyncMock()
        client.get_spread = AsyncMock(return_value=1.0)
        client.get_expected_margin = AsyncMock(return_value=500.0)
        client.get_positions = AsyncMock(return_value=[])
        client.get_balance = AsyncMock(return_value=10000.0)
        orchestrator = AsyncMock()

        await engine.on_bar_update(state, orchestrator, client, None)

        client.place_limit_order.assert_not_called()


def _eurusd_pair_info():
    return (1, 100_000, 5, 0.0001, 100_000, 100_000, 10_000_000)


def _make_resolved_volume(lot: float = 0.01):
    from app.trading.volume import ResolvedVolume, lot_to_volume_cents

    cents = lot_to_volume_cents(lot)
    return ResolvedVolume(
        calculated_lot=lot,
        calculated_volume_cents=cents,
        actual_volume_cents=cents,
        min_volume_cents=100_000,
        step_volume_cents=100_000,
        bumped_to_min=False,
        rounded_to_step=False,
    )


class TestTradingEngineLifecycle:
    """Reconciliation, TTL, orders, degradation transitions."""

    async def test_reconcile_removes_stale_position(self):
        engine = _make_engine("EURUSD")
        eur = engine.grids.get("EURUSD")
        eur.long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1,
            volume=0.01,
            stop_loss=1.09,
            client_order_id="ord-1",
            position_id="999",
        ))

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_spread = AsyncMock(return_value=1.0)
        client.get_pair_info = MagicMock(return_value=_eurusd_pair_info())

        await engine._reconcile_positions(client, "EURUSD")

        assert eur.long_grid.level_count() == 0

    async def test_reconcile_restores_from_pending_metadata(self):
        engine = _make_engine("EURUSD")
        placed_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
        engine.pending_order_metadata["pending-1"] = {
            "level_index": 1,
            "grid_step_at_open": 0.0015,
            "signal_score_at_open": 8.0,
            "order_placed_at": placed_at,
            "direction": Direction.LONG,
            "entry_price": 1.1000,
            "pair": "EURUSD",
        }

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[{
            "id": 42,
            "symbol": "EURUSD",
            "volume": 100_000,
            "entry_price": 1.1000,
            "profit": 0.0,
            "direction": "BUY",
        }])
        client.get_spread = AsyncMock(return_value=0.0002)
        client.get_pair_info = MagicMock(return_value=_eurusd_pair_info())

        await engine._reconcile_positions(client, "EURUSD")

        eur = engine.grids.get("EURUSD")
        assert eur.long_grid.level_count() == 1
        pos = eur.long_grid.get_last_position()
        assert pos.position_id == "42"
        assert pos.level_index == 1
        assert "pending-1" not in engine.pending_order_metadata

    async def test_bootstrap_restores_broker_positions(self):
        engine = _make_engine("EURUSD", "XAUUSD")

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[
            {
                "id": 642701763,
                "symbol": "BTCUSD",
                "volume": 1,
                "entry_price": 63922.39,
                "direction": "SELL",
            },
            {
                "id": 100,
                "symbol": "EURUSD",
                "volume": 100_000,
                "entry_price": 1.1,
                "direction": "BUY",
            },
        ])
        client.get_pending_orders = AsyncMock(return_value=[])
        client.get_pair_info = MagicMock(return_value=_eurusd_pair_info())

        orchestrator = AsyncMock()
        await engine.bootstrap_from_broker(client, orchestrator)

        eur = engine.grids.get("EURUSD")
        assert eur.short_grid.level_count() == 0
        assert eur.long_grid.level_count() == 1
        pos = eur.long_grid.get_position_by_id("100")
        assert pos is not None
        assert pos.pair == "EURUSD"
        assert pos.volume == 0.01

    async def test_on_order_filled_links_position_id(self):
        engine = _make_engine("EURUSD")
        eur = engine.grids.get("EURUSD")
        eur.long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1,
            volume=0.01,
            stop_loss=1.09,
            client_order_id="ord-abc",
            pair="EURUSD",
        ))
        engine.pending_order_metadata["ord-abc"] = {"pair": "EURUSD"}

        engine.on_order_filled("ord-abc", "555", "EURUSD")

        pos = eur.long_grid.get_position_by_client_order_id("ord-abc")
        assert pos.position_id == "555"
        assert "ord-abc" not in engine.pending_order_metadata

    async def test_ttl_cancels_expired_pending(self):
        from app.config.settings import config
        from app.models.candle import Candle

        engine = _make_engine("EURUSD")
        old_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        engine.pending_order_metadata["expired-1"] = {
            "order_placed_at": old_time,
            "pair": "EURUSD",
            "direction": Direction.LONG,
            "entry_price": 1.1,
            "level_index": 1,
            "grid_step_at_open": 0.001,
            "signal_score_at_open": 7.0,
        }
        engine.grids.get("EURUSD").long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1,
            volume=0.01,
            stop_loss=1.09,
            client_order_id="expired-1",
        ))

        client = AsyncMock()
        client.cancel_order_by_client_id = AsyncMock(return_value=True)
        candles = [Candle(
            timestamp=datetime(2024, 6, 1, tzinfo=timezone.utc),
            open=1.1, high=1.11, low=1.09, close=1.105, volume=100,
        )]

        await engine._check_pending_orders_ttl(client, "EURUSD", candles, entry_minutes=5)

        client.cancel_order_by_client_id.assert_awaited_once_with("expired-1", timeout=5)
        assert "expired-1" not in engine.pending_order_metadata
        assert engine.grids.get("EURUSD").long_grid.level_count() == 0

    async def test_place_grid_order_track_failure_does_not_add_position(self):
        engine = _make_engine("EURUSD")
        grid = engine.grids.get("EURUSD").long_grid
        client = AsyncMock()
        client.get_pair_info = MagicMock(return_value=_eurusd_pair_info())
        client.get_pending_orders = AsyncMock(return_value=[])
        client.place_limit_order = AsyncMock(return_value=("new-order", _make_resolved_volume()))
        orchestrator = AsyncMock()
        orchestrator.track_limit_order = AsyncMock(side_effect=RuntimeError("track failed"))

        ok = await engine._place_grid_order(
            direction=Direction.LONG,
            current_price=1.1,
            current_atr=0.001,
            volume=0.01,
            score=8.0,
            orchestrator=orchestrator,
            client=client,
            pair="EURUSD",
            symbol_id=1,
            grid_manager=grid,
        )

        assert ok is False
        assert grid.level_count() == 0
        assert "new-order" in engine.pending_order_metadata

    async def test_place_grid_order_success(self):
        engine = _make_engine("EURUSD")
        grid = engine.grids.get("EURUSD").long_grid
        client = AsyncMock()
        client.get_pair_info = MagicMock(return_value=_eurusd_pair_info())
        client.get_pending_orders = AsyncMock(return_value=[])
        client.place_limit_order = AsyncMock(return_value=("ok-order", _make_resolved_volume()))
        orchestrator = AsyncMock()

        ok = await engine._place_grid_order(
            direction=Direction.LONG,
            current_price=1.1,
            current_atr=0.001,
            volume=0.01,
            score=8.0,
            orchestrator=orchestrator,
            client=client,
            pair="EURUSD",
            symbol_id=1,
            grid_manager=grid,
        )

        assert ok is True
        assert grid.level_count() == 1
        assert grid.get_last_position().client_order_id == "ok-order"
        orchestrator.track_limit_order.assert_awaited_once()

    async def test_freeze_transition_cancels_pending(self):
        engine = _make_engine("EURUSD")
        engine._last_degradation_mode = DegradationMode.NORMAL
        engine.pending_order_metadata["pend-1"] = {
            "pair": "EURUSD",
            "order_placed_at": datetime.now(timezone.utc),
            "direction": Direction.LONG,
            "entry_price": 1.1,
            "level_index": 1,
            "grid_step_at_open": 0.001,
            "signal_score_at_open": 7.0,
        }

        client = AsyncMock()
        client.get_pending_orders = AsyncMock(return_value=[
            {"clientOrderId": "pend-1"},
        ])
        client.cancel_order_by_client_id = AsyncMock(return_value=True)
        orchestrator = AsyncMock()
        orchestrator.cancel_tracked_limit_order = AsyncMock(return_value=False)

        await engine._handle_degradation_transition(
            DegradationMode.FREEZE, client, orchestrator, "EURUSD",
        )

        assert "pend-1" not in engine.pending_order_metadata
        client.cancel_order_by_client_id.assert_awaited()

    async def test_process_grid_signal_respects_manual_mode(self, monkeypatch):
        from types import SimpleNamespace
        from app.config.settings import config as app_config
        from app.trading import trading_engine as te

        patched = SimpleNamespace(
            TRADING_MODE="MANUAL",
            TRADE_WINDOW_START=app_config.TRADE_WINDOW_START,
            TRADE_WINDOW_END=app_config.TRADE_WINDOW_END,
            ENTRY_THRESHOLD=app_config.ENTRY_THRESHOLD,
            ADD_LEVEL_THRESHOLD=app_config.ADD_LEVEL_THRESHOLD,
            CONSERVATIVE_SCORE_PENALTY=app_config.CONSERVATIVE_SCORE_PENALTY,
            IMBALANCE_SOFT_THRESHOLD=app_config.IMBALANCE_SOFT_THRESHOLD,
            IMBALANCE_SCORE_PENALTY=app_config.IMBALANCE_SCORE_PENALTY,
            MAX_GRID_LEVELS=app_config.MAX_GRID_LEVELS,
            SL_ATR_MULTIPLIER=app_config.SL_ATR_MULTIPLIER,
            RISK_PER_TRADE=app_config.RISK_PER_TRADE,
            CONSERVATIVE_VOLUME_MULTIPLIER=app_config.CONSERVATIVE_VOLUME_MULTIPLIER,
            MAX_LOT=app_config.MAX_LOT,
            ATR_MULTIPLIER=app_config.ATR_MULTIPLIER,
        )

        engine = _make_engine("EURUSD")
        monkeypatch.setattr(te, "config", patched)
        client = AsyncMock()
        orchestrator = AsyncMock()

        await engine._process_grid_signal(
            direction=Direction.LONG,
            score=9.0,
            current_price=1.1,
            current_atr=0.001,
            degradation_mode=DegradationMode.NORMAL,
            orchestrator=orchestrator,
            client=client,
            pair="EURUSD",
            symbol_id=1,
            equity=10_000.0,
            margin_per_lot=1000.0,
        )

        client.place_limit_order.assert_not_called()

    async def test_close_position_retries_once(self):
        engine = _make_engine("EURUSD")
        client = AsyncMock()
        client.close_position_partial = AsyncMock(return_value=True)
        client.get_positions = AsyncMock(side_effect=[
            [{"id": 7, "volume": 50_000, "symbol": "EURUSD"}],
            [],
        ])

        ok = await engine._close_position_with_verify(client, "7", 100_000)

        assert ok is True
        assert client.close_position_partial.await_count == 2


class TestPositionSizing:
    """Risk-based lot calculation for Dual Grid."""

    async def test_calculate_position_size_eurusd_realistic(self):
        """~1480 USD equity, 2% risk, ATR-based SL → well below MAX_LOT."""
        from app.config.settings import config

        engine = _make_engine("EURUSD")
        engine.portfolio.initialize_baseline(1479.98)

        client = AsyncMock()
        client.get_balance = AsyncMock(return_value=1479.98)
        client.get_pair_info = MagicMock(return_value=(1, 100_000, 5, 0.0001, 100_000, 100_000, 10_000_000))

        volume = await engine._calculate_position_size(
            client, "EURUSD", current_atr=0.0008, degradation_mode=DegradationMode.NORMAL,
            equity=1479.98,
        )

        # risk≈29.6, sl_dist=0.0016 → raw≈0.185 lot
        assert 0.01 <= volume < 0.5
        assert volume <= config.MAX_LOT

    async def test_calculate_position_size_not_capped_at_max_lot(self):
        """Broken formula would hit MAX_LOT=1.0; fixed formula must not."""
        from app.config.settings import config

        engine = _make_engine("EURUSD")
        engine.portfolio.initialize_baseline(1479.98)

        client = AsyncMock()
        client.get_balance = AsyncMock(return_value=1479.98)
        client.get_pair_info = MagicMock(return_value=(1, 100_000, 5, 0.0001, 100_000, 100_000, 10_000_000))

        volume = await engine._calculate_position_size(
            client, "EURUSD", current_atr=0.0008, degradation_mode=DegradationMode.NORMAL,
            equity=1479.98,
        )

        assert volume < 1.0 or config.MAX_LOT < 1.0

    async def test_calculate_position_size_independent_of_prior_pnl(self):
        """Volume depends on equity/ATR, not previous level PnL (no martingale)."""
        engine = _make_engine("EURUSD")
        engine.portfolio.initialize_baseline(10_000.0)

        client = AsyncMock()
        client.get_balance = AsyncMock(return_value=10_000.0)
        client.get_pair_info = MagicMock(return_value=(1, 100_000, 5, 0.0001, 100_000, 100_000, 10_000_000))

        vol_a = await engine._calculate_position_size(
            client, "EURUSD", current_atr=0.0010, degradation_mode=DegradationMode.NORMAL,
            equity=10_000.0,
        )
        vol_b = await engine._calculate_position_size(
            client, "EURUSD", current_atr=0.0010, degradation_mode=DegradationMode.NORMAL,
            equity=10_000.0,
        )

        assert vol_a == vol_b
        assert vol_a > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
