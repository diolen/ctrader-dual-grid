"""Tests for Trading Layer - Dual Grid Strategy."""

import pytest
import pandas as pd
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, MagicMock

from app.trading.grid_models import GridPosition, Direction, ExecutionApproval, DegradationMode
from app.trading.grid_manager import GridManager
from app.trading.portfolio_manager import PortfolioManager
from app.trading.atr_calculator import compute_atr, compute_atr_baseline
from app.trading.trading_engine import TradingEngine, RollingWindow


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
        """Test can_expand with exposure limit."""
        manager = PortfolioManager()
        manager.initialize_baseline(10000.0)
        
        long_grid = GridManager(Direction.LONG)
        short_grid = GridManager(Direction.SHORT)
        
        # Add positions that exceed limit
        long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=10.0,  # Large volume
            stop_loss=1.0900,
            client_order_id="test_1",
        ))
        
        # With margin_per_lot = 1000, max_lots = (10000 * 0.5) / 1000 = 5
        manager.margin_per_lot = 1000.0
        
        result = manager.can_expand(Direction.LONG, long_grid, short_grid, 10000.0, 1000.0)
        assert result is False
    
    def test_can_expand_imbalance_hard_threshold(self):
        """Test can_expand with imbalance hard threshold."""
        from app.config.settings import config
        
        manager = PortfolioManager()
        manager.initialize_baseline(10000.0)
        manager.margin_per_lot = 1000.0
        
        long_grid = GridManager(Direction.LONG)
        short_grid = GridManager(Direction.SHORT)
        
        # Create imbalance: long has 8 lots, short has 2 lots
        # Total = 10, imbalance = |8-2|/10 = 0.6
        long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=8.0,
            stop_loss=1.0900,
            client_order_id="long_1",
        ))
        short_grid.add_position(GridPosition(
            direction=Direction.SHORT,
            entry_price=1.1000,
            volume=2.0,
            stop_loss=1.1100,
            client_order_id="short_1",
        ))
        
        # Try to add more to overweight side (LONG)
        result = manager.can_expand(Direction.LONG, long_grid, short_grid, 10000.0, 1000.0)
        assert result is False
    
    def test_get_add_level_threshold_adjustment(self):
        """Test score threshold adjustment based on imbalance."""
        from app.config.settings import config
        
        manager = PortfolioManager()
        
        long_grid = GridManager(Direction.LONG)
        short_grid = GridManager(Direction.SHORT)
        
        # No imbalance
        adjustment = manager.get_add_level_threshold_adjustment(long_grid, short_grid)
        assert adjustment == 0.0
        
        # Create imbalance
        long_grid.add_position(GridPosition(
            direction=Direction.LONG,
            entry_price=1.1000,
            volume=8.0,
            stop_loss=1.0900,
            client_order_id="long_1",
        ))
        short_grid.add_position(GridPosition(
            direction=Direction.SHORT,
            entry_price=1.1000,
            volume=2.0,
            stop_loss=1.1100,
            client_order_id="short_1",
        ))
        
        adjustment = manager.get_add_level_threshold_adjustment(long_grid, short_grid)
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
        engine = TradingEngine()
        
        assert engine.long_grid.direction == Direction.LONG
        assert engine.short_grid.direction == Direction.SHORT
        assert engine.portfolio.baseline_equity == 0.0
        assert len(engine.pending_order_metadata) == 0
    
    def test_restart(self):
        """Test TradingEngine restart."""
        engine = TradingEngine()
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
        
        engine = TradingEngine()
        
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
    
    def test_execution_check_cold_start(self):
        """Test execution check passes on cold start (window not full)."""
        engine = TradingEngine()
        
        # Mock client
        client = Mock()
        client.get_spread = Mock(return_value=1.5)
        
        approval = engine._execution_check("EURUSD", client, current_atr=0.0010)
        
        # Should approve with no reduction on cold start
        assert approval.approved is True
        assert approval.volume_multiplier == 1.0
    
    def test_execution_check_spread_rejection(self):
        """Test execution check rejects on high spread."""
        engine = TradingEngine()
        
        # Fill spread history
        for _ in range(20):
            engine.spread_history["EURUSD"].add(1.0)
        
        # Fill ATR history
        for _ in range(50):
            engine.atr_baseline_history["EURUSD"].add(0.0010)
        
        client = Mock()
        client.get_spread = Mock(return_value=3.0)  # 3x average
        
        approval = engine._execution_check("EURUSD", client, current_atr=0.0010)
        
        assert approval.approved is False
    
    def test_execution_check_warn_zone(self):
        """Test execution check reduces volume in warn zone."""
        from app.config.settings import config
        
        engine = TradingEngine()
        
        # Fill spread history
        for _ in range(20):
            engine.spread_history["EURUSD"].add(1.0)
        
        # Fill ATR history
        for _ in range(50):
            engine.atr_baseline_history["EURUSD"].add(0.0010)
        
        client = Mock()
        client.get_spread = Mock(return_value=1.8)  # In warn zone
        
        approval = engine._execution_check("EURUSD", client, current_atr=0.0010)
        
        assert approval.approved is True
        assert approval.volume_multiplier == config.EXECUTION_WARN_VOLUME_REDUCTION


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
