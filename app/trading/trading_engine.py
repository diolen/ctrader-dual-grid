"""Trading Engine for Dual Grid Strategy - main orchestrator."""

import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import deque

from app.trading.grid_models import GridPosition, Direction, ExecutionApproval, DegradationMode
from app.trading.grid_manager import GridManager
from app.trading.portfolio_manager import PortfolioManager
from app.trading.atr_calculator import compute_atr, compute_atr_baseline
from app.config.settings import config
from app.scanner.engine.setup_scanner_engine import SetupScannerEngine
from app.scanner.context.builder import MarketContextBuilder
from app.scanner.types.setup_candidate import SetupCandidate
from app.scanner.types.enums import Direction as ScannerDirection


logger = logging.getLogger(__name__)


class RollingWindow:
    """Simple rolling window for storing history."""
    
    def __init__(self, max_size: int):
        self.max_size = max_size
        self.window: deque = deque(maxlen=max_size)
    
    def add(self, value: float) -> None:
        self.window.append(value)
    
    def mean(self) -> float:
        if not self.window:
            return 0.0
        return sum(self.window) / len(self.window)
    
    def is_full(self) -> bool:
        return len(self.window) == self.max_size


class TradingEngine:
    """Main trading engine for Dual Grid Strategy."""
    
    def __init__(self):
        # Configuration
        self.watched_instruments = config.WATCHED_INSTRUMENTS
        
        # Grid managers
        self.long_grid = GridManager(Direction.LONG)
        self.short_grid = GridManager(Direction.SHORT)
        
        # Portfolio manager
        self.portfolio = PortfolioManager()
        
        # Internal state
        self.spread_history: Dict[str, RollingWindow] = {}
        self.atr_baseline_history: Dict[str, RollingWindow] = {}
        self.pending_order_metadata: Dict[str, Dict[str, Any]] = {}
        
        # Scanner engine
        self.scanner = SetupScannerEngine()
        
        # Initialize rolling windows for watched instruments
        for symbol, _ in self.watched_instruments:
            self.spread_history[symbol] = RollingWindow(config.SPREAD_LOOKBACK_BARS)
            self.atr_baseline_history[symbol] = RollingWindow(config.ATR_BASELINE_LOOKBACK_BARS)
    
    def restart(self, equity: float) -> None:
        """Restart after halt - reset state."""
        self.portfolio.restart(equity)
        # Keep spread_history and atr_baseline_history as per spec
        self.pending_order_metadata.clear()
        logger.info("TradingEngine: restarted")
    
    def on_bar_update(
        self,
        state: Any,
        orchestrator: Any,
        client: Any,
        market_cache: Any,
    ) -> None:
        """
        Main entry point called on each bar update for a single symbol.
        
        Args:
            state: _PairRuntime with pair, symbol_id, candles, entry_tf, etc.
            orchestrator: StrategyOrchestrator instance
            client: CTraderClient instance
            market_cache: MarketCache instance
        """
        pair = state.pair
        symbol_id = state.symbol_id
        candles = state.candles
        entry_tf = state.entry_tf
        
        # Step 0: Reconciliation with broker
        self._reconcile_positions(client, pair)
        
        # Step 0.5: TTL check for pending orders
        self._check_pending_orders_ttl(client, pair, candles, state.entry_minutes)
        
        # Step 1: Get current spread and update history
        current_spread = client.get_spread(pair)
        self.spread_history[pair].add(current_spread)
        
        # Step 1: Cache margin per lot if not already cached
        if self.portfolio.margin_per_lot == 0.0:
            margin = client.get_expected_margin(symbol_id, volume_cents=100_000)
            if margin is None:
                logger.critical(f"TradingEngine: get_expected_margin returned None for {pair}")
                return
            self.portfolio.margin_per_lot = margin
            logger.info(f"TradingEngine: cached margin_per_lot={margin:.2f} for {pair}")
        
        # Step 2: Convert candles to DataFrame and calculate ATR
        candles_df = self._candles_to_dataframe(candles)
        current_atr = compute_atr(candles_df, config.ATR_PERIOD)
        self.atr_baseline_history[pair].add(current_atr)
        atr_baseline = compute_atr_baseline(list(self.atr_baseline_history[pair].window))
        
        # Step 3: Check if halted
        if self.portfolio.halted:
            logger.debug(f"TradingEngine: halted, skipping {pair}")
            return
        
        # Step 4: Check global TP/SL
        positions = client.get_positions(force=True)
        total_profit = sum(p.get('profit', 0) for p in positions.values())
        equity = client.get_balance()
        
        # Initialize baseline on first run
        self.portfolio.initialize_baseline(equity)
        
        portfolio_pnl_fraction = self.portfolio.calculate_portfolio_pnl_fraction(total_profit)
        tp_sl_result = self.portfolio.check_global_tp_sl(portfolio_pnl_fraction)
        
        if tp_sl_result:
            logger.warning(f"TradingEngine: Global {tp_sl_result} hit, closing all positions")
            self._close_all_positions(client, orchestrator, pair)
            self.portfolio.halted = True
            return
        
        # Step 5: Determine degradation mode
        degradation_mode = self.portfolio.determine_degradation_mode(
            portfolio_pnl_fraction, current_atr, atr_baseline
        )
        
        if degradation_mode == DegradationMode.EXIT:
            logger.warning(f"TradingEngine: EXIT mode, closing worst position")
            self._close_worst_position(client, orchestrator, pair)
            return
        
        # Step 6: Build MarketContext
        context = MarketContextBuilder.build(pair, entry_tf, candles_df)
        
        # Step 7: Scan for setups
        candidates = self.scanner.scan(candles_df, context)
        
        if not candidates:
            # No signals - normal situation, no action
            return
        
        # Step 8: Process first candidate (highest score)
        candidate = candidates[0]
        
        # Step 9: Map BUY/SELL to LONG/SHORT and apply grid logic
        scanner_direction = candidate.direction
        if scanner_direction == ScannerDirection.BUY:
            direction = Direction.LONG
        elif scanner_direction == ScannerDirection.SELL:
            direction = Direction.SHORT
        else:
            logger.warning(f"TradingEngine: unknown scanner direction {scanner_direction}")
            return
        
        # Step 9: Apply grid logic
        self._process_grid_signal(
            direction=direction,
            score=candidate.score,
            current_price=candles_df['close'].iloc[-1],
            current_atr=current_atr,
            degradation_mode=degradation_mode,
            orchestrator=orchestrator,
            client=client,
            pair=pair,
            symbol_id=symbol_id,
            entry_tf=entry_tf,
        )
    
    def _candles_to_dataframe(self, candles: List) -> pd.DataFrame:
        """Convert List[Candle] to pandas DataFrame."""
        data = []
        for candle in candles:
            data.append({
                'timestamp': candle.timestamp,
                'open': candle.open,
                'high': candle.high,
                'low': candle.low,
                'close': candle.close,
                'volume': candle.volume,
            })
        return pd.DataFrame(data)
    
    def _reconcile_positions(self, client: Any, pair: str) -> None:
        """Reconcile GridManager positions with broker state."""
        broker_positions = client.get_positions(force=False)
        
        # Remove positions from memory that don't exist at broker
        for grid_manager in [self.long_grid, self.short_grid]:
            to_remove = []
            for pos in grid_manager.positions:
                if pos.position_id and pos.position_id not in broker_positions:
                    to_remove.append(pos.position_id)
            
            for pos_id in to_remove:
                grid_manager.remove_position(pos_id)
                logger.info(f"TradingEngine: removed stale position {pos_id} from memory")
        
        # Add positions that exist at broker but not in memory
        for pos_id, pos_data in broker_positions.items():
            direction = Direction.LONG if pos_data.get('direction') == 'LONG' else Direction.SHORT
            grid_manager = self.long_grid if direction == Direction.LONG else self.short_grid
            
            if not grid_manager.get_position_by_id(pos_id):
                # Try to match with pending_order_metadata
                matched = self._match_pending_order_metadata(pos_data, direction)
                if matched:
                    # Add position from metadata
                    position = GridPosition(
                        direction=direction,
                        entry_price=pos_data.get('entry_price', 0),
                        volume=pos_data.get('volume', 0),
                        stop_loss=pos_data.get('stop_loss', 0),
                        client_order_id=matched['client_order_id'],
                        position_id=pos_id,
                        order_placed_at=matched['order_placed_at'],
                        position_opened_at=datetime.utcnow(),
                        grid_step_at_open=matched['grid_step_at_open'],
                        level_index=matched['level_index'],
                        signal_score_at_open=matched['signal_score_at_open'],
                    )
                    grid_manager.add_position(position)
                    # Remove from metadata
                    del self.pending_order_metadata[matched['client_order_id']]
                    logger.info(f"TradingEngine: restored position {pos_id} from pending metadata")
                else:
                    logger.warning(f"TradingEngine: unmatched position {pos_id} at broker")
    
    def _match_pending_order_metadata(self, pos_data: Dict, direction: Direction) -> Optional[Dict]:
        """Match broker position with pending_order_metadata."""
        entry_price = pos_data.get('entry_price', 0)
        current_spread = 0.0  # Would need to get this from somewhere
        
        for client_order_id, metadata in self.pending_order_metadata.items():
            if metadata['direction'] != direction:
                continue
            
            # Check if entry price matches within tolerance (half spread)
            price_diff = abs(entry_price - metadata.get('entry_price', 0))
            if price_diff < current_spread / 2:
                return metadata
        
        return None
    
    def _check_pending_orders_ttl(self, client: Any, pair: str, candles: List, entry_minutes: int) -> None:
        """Check TTL for pending grid orders."""
        if not candles:
            return
        
        current_bar = candles[-1]
        current_bar_ms = int(current_bar.timestamp.timestamp() * 1000)
        bar_duration_ms = entry_minutes * 60 * 1000
        
        to_cancel = []
        for client_order_id, metadata in self.pending_order_metadata.items():
            order_placed_ms = int(metadata['order_placed_at'].timestamp() * 1000)
            elapsed_ms = current_bar_ms - order_placed_ms
            
            if elapsed_ms > config.GRID_ORDER_TTL_BARS * bar_duration_ms:
                to_cancel.append(client_order_id)
        
        for client_order_id in to_cancel:
            try:
                client.cancel_order_by_client_id(client_order_id, timeout=5)
                del self.pending_order_metadata[client_order_id]
                logger.info(f"TradingEngine: cancelled expired pending order {client_order_id}")
            except Exception as e:
                logger.error(f"TradingEngine: failed to cancel expired order {client_order_id}: {e}")
    
    def _process_grid_signal(
        self,
        direction: Direction,
        score: float,
        current_price: float,
        current_atr: float,
        degradation_mode: str,
        orchestrator: Any,
        client: Any,
        pair: str,
        symbol_id: int,
        entry_tf: str,
    ) -> None:
        """Process a signal for grid entry or level addition."""
        grid_manager = self.long_grid if direction == Direction.LONG else self.short_grid
        
        # Check if can open/add based on grid state
        if not grid_manager.is_active():
            # Opening first position
            if score < config.ENTRY_THRESHOLD:
                return
        else:
            # Adding level
            if not self._can_add_level(grid_manager, current_price, current_atr, degradation_mode):
                return
        
        # Calculate position size
        volume = self._calculate_position_size(client, pair, current_atr, degradation_mode)
        if volume <= 0:
            return
        
        # Execution check
        approval = self._execution_check(pair, client, current_atr)
        if not approval.approved:
            self.portfolio.increment_execution_rejection()
            return
        
        volume *= approval.volume_multiplier
        
        # Place order
        self._place_grid_order(
            direction=direction,
            current_price=current_price,
            current_atr=current_atr,
            volume=volume,
            score=score,
            orchestrator=orchestrator,
            client=client,
            pair=pair,
            symbol_id=symbol_id,
            grid_manager=grid_manager,
        )
        
        # Reset execution rejections on success
        self.portfolio.reset_execution_rejections()
    
    def _can_add_level(
        self,
        grid_manager: GridManager,
        current_price: float,
        current_atr: float,
        degradation_mode: str,
    ) -> bool:
        """Check if can add a new level to existing grid."""
        last_position = grid_manager.get_last_position()
        if not last_position:
            return False
        
        # Check distance from last position
        distance = abs(current_price - last_position.entry_price)
        if distance < last_position.grid_step_at_open:
            return False
        
        # Check max levels
        if not grid_manager.can_add_level(config.MAX_GRID_LEVELS):
            return False
        
        # Check degradation mode
        if degradation_mode in [DegradationMode.FREEZE, DegradationMode.EXIT]:
            return False
        
        return True
    
    def _calculate_position_size(
        self,
        client: Any,
        pair: str,
        current_atr: float,
        degradation_mode: str,
    ) -> float:
        """Calculate position size based on risk and ATR."""
        equity = client.get_balance()
        
        # Guard checks
        if self.portfolio.baseline_equity == 0:
            logger.critical("baseline_equity == 0, halting system")
            self.portfolio.halted = True
            return 0.0
        
        sl_distance_price = current_atr * config.SL_ATR_MULTIPLIER
        if sl_distance_price == 0:
            logger.warning("sl_distance_price == 0, execution rejection")
            self.portfolio.increment_execution_rejection()
            return 0.0
        
        # Get pip_value
        pair_info = client.get_pair_info(pair)
        if not pair_info or len(pair_info) <= 3:
            logger.critical(f"get_pair_info failed for {pair}")
            return 0.0
        
        pip_value = float(pair_info[3])
        if pip_value == 0:
            logger.warning("pip_value == 0, execution rejection")
            self.portfolio.increment_execution_rejection()
            return 0.0
        
        # Calculate raw volume
        risk_amount = equity * config.RISK_PER_TRADE
        raw_volume = risk_amount / (sl_distance_price * pip_value)
        
        # Apply degradation mode adjustments
        if degradation_mode == DegradationMode.CONSERVATIVE:
            raw_volume *= config.CONSERVATIVE_VOLUME_MULTIPLIER
        
        # Round to step volume and clamp
        # These values would come from broker, using defaults for now
        step_volume = 0.01
        min_volume = 0.01
        max_volume = config.MAX_LOT
        
        volume = (raw_volume // step_volume) * step_volume
        volume = max(min_volume, min(volume, max_volume))
        
        return volume
    
    def _execution_check(self, pair: str, client: Any, current_atr: float) -> ExecutionApproval:
        """Perform execution check based on spread and ATR."""
        spread_window = self.spread_history[pair]
        atr_window = self.atr_baseline_history[pair]
        
        current_spread = client.get_spread(pair)
        
        # If windows not full, skip check (cold start)
        if not spread_window.is_full() or not atr_window.is_full():
            return ExecutionApproval(approved=True, volume_multiplier=1.0)
        
        avg_spread = spread_window.mean()
        atr_baseline = atr_window.mean()
        
        # Check spread rejection
        if current_spread > config.SPREAD_REJECT_MULTIPLIER * avg_spread:
            logger.warning(f"Execution check failed: spread {current_spread:.2f} > {config.SPREAD_REJECT_MULTIPLIER * avg_spread:.2f}")
            return ExecutionApproval(approved=False)
        
        # Check ATR spike rejection
        if current_atr > config.ATR_SPIKE_MULTIPLIER * atr_baseline:
            logger.warning(f"Execution check failed: ATR {current_atr:.5f} > {config.ATR_SPIKE_MULTIPLIER * atr_baseline:.5f}")
            return ExecutionApproval(approved=False)
        
        # Check warn zones
        volume_multiplier = 1.0
        if current_spread > config.SPREAD_WARN_MULTIPLIER * avg_spread:
            volume_multiplier *= config.EXECUTION_WARN_VOLUME_REDUCTION
            logger.info(f"Execution warn: spread elevated, volume reduced to {volume_multiplier:.2f}")
        
        if current_atr > config.ATR_SPIKE_MULTIPLIER * atr_baseline:
            volume_multiplier *= config.EXECUTION_WARN_VOLUME_REDUCTION
            logger.info(f"Execution warn: ATR elevated, volume reduced to {volume_multiplier:.2f}")
        
        return ExecutionApproval(approved=True, volume_multiplier=volume_multiplier)
    
    def _place_grid_order(
        self,
        direction: Direction,
        current_price: float,
        current_atr: float,
        volume: float,
        score: float,
        orchestrator: Any,
        client: Any,
        pair: str,
        symbol_id: int,
        grid_manager: GridManager,
    ) -> None:
        """Place a grid order."""
        # Calculate grid step
        grid_step = current_atr * config.ATR_MULTIPLIER
        
        # Calculate stop loss
        if direction == Direction.LONG:
            stop_loss = current_price - grid_step * config.SL_ATR_MULTIPLIER
            broker_direction = "BUY"
        else:
            stop_loss = current_price + grid_step * config.SL_ATR_MULTIPLIER
            broker_direction = "SELL"
        
        # Check for duplicate pending orders
        pending_orders = client.get_pending_orders(force=True)
        for order_id in pending_orders:
            if order_id in self.pending_order_metadata:
                logger.warning(f"Duplicate pending order detected: {order_id}, skipping")
                return
        
        # Place limit order
        result = client.place_limit_order(
            symbol_id=symbol_id,
            direction=broker_direction,
            lot=volume,
            entry=current_price,
            stop_loss=stop_loss,
            multiplier=1,  # Not used per spec
            min_volume=0.01,
            step_volume=0.01,
            pair=pair,
        )
        
        if result is None:
            logger.error(f"place_limit_order returned None for {pair}")
            self.portfolio.increment_execution_rejection()
            return
        
        client_order_id, resolved_volume = result
        
        # Create GridPosition
        level_index = grid_manager.level_count() + 1
        position = GridPosition(
            direction=direction,
            entry_price=current_price,
            volume=resolved_volume,
            stop_loss=stop_loss,
            client_order_id=client_order_id,
            order_placed_at=datetime.utcnow(),
            grid_step_at_open=grid_step,
            level_index=level_index,
            signal_score_at_open=score,
        )
        
        # Add to grid manager
        grid_manager.add_position(position)
        
        # Store metadata
        self.pending_order_metadata[client_order_id] = {
            'level_index': level_index,
            'grid_step_at_open': grid_step,
            'signal_score_at_open': score,
            'order_placed_at': datetime.utcnow(),
            'direction': direction,
            'entry_price': current_price,
        }
        
        # Track with orchestrator
        try:
            orchestrator.track_limit_order(pair, client_order_id)
        except Exception as e:
            logger.critical(f"Failed to track limit order {client_order_id}: {e}")
            # Order is placed at broker but orchestrator doesn't know about it
            # This is handled in reconciliation
        
        logger.info(
            f"TradingEngine: placed grid order {client_order_id} for {pair}, "
            f"direction={direction.value}, level={level_index}, volume={resolved_volume:.2f}"
        )
    
    def _close_all_positions(self, client: Any, orchestrator: Any, pair: str) -> None:
        """Close all positions for a pair."""
        # First, cancel pending orders
        self._cancel_all_pending_orders(client, orchestrator, pair)
        
        # Then close open positions
        positions = client.get_positions(force=True)
        for pos_id, pos_data in positions.items():
            try:
                volume_cents = int(pos_data.get('volume', 0) * 100_000)
                client.close_position_partial(pos_id, volume_cents, timeout=10)
                logger.info(f"TradingEngine: closed position {pos_id}")
            except Exception as e:
                logger.error(f"TradingEngine: failed to close position {pos_id}: {e}")
        
        # Verify all positions are closed
        positions_after = client.get_positions(force=True)
        if positions_after:
            logger.critical(f"TradingEngine: positions still exist after close_all: {list(positions_after.keys())}")
    
    def _close_worst_position(self, client: Any, orchestrator: Any, pair: str) -> None:
        """Close the worst (least profitable) position."""
        positions = client.get_positions(force=True)
        
        if not positions:
            return
        
        # Find position with minimum profit
        worst_pos_id = min(positions.keys(), key=lambda k: positions[k].get('profit', 0))
        
        try:
            volume_cents = int(positions[worst_pos_id].get('volume', 0) * 100_000)
            client.close_position_partial(worst_pos_id, volume_cents, timeout=10)
            logger.info(f"TradingEngine: closed worst position {worst_pos_id}")
        except Exception as e:
            logger.error(f"TradingEngine: failed to close worst position {worst_pos_id}: {e}")
        
        # Verify position is closed
        positions_after = client.get_positions(force=True)
        if worst_pos_id in positions_after:
            logger.critical(f"TradingEngine: worst position {worst_pos_id} still exists after close")
    
    def _cancel_all_pending_orders(self, client: Any, orchestrator: Any, pair: str) -> None:
        """Cancel all pending grid orders for a pair."""
        for client_order_id, metadata in list(self.pending_order_metadata.items()):
            try:
                # Try to cancel through orchestrator first
                orchestrator.cancel_tracked_limit_order(pair)
            except Exception:
                # If orchestrator doesn't know about it, cancel directly
                try:
                    client.cancel_order_by_client_id(client_order_id, timeout=5)
                    logger.warning(f"TradingEngine: cancelled untracked order {client_order_id}")
                except Exception as e:
                    logger.error(f"TradingEngine: failed to cancel order {client_order_id}: {e}")
            
            # Remove from metadata
            del self.pending_order_metadata[client_order_id]
