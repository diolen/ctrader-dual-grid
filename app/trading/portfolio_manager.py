"""Portfolio Manager for Trading Layer - portfolio-level risk management."""

import logging
from typing import Optional

from app.trading.grid_models import Direction, DegradationMode
from app.trading.grid_manager import GridManager
from app.config.settings import config


logger = logging.getLogger(__name__)


class PortfolioManager:
    """Manages portfolio-level decisions and risk limits."""
    
    def __init__(self):
        self.baseline_equity: float = 0.0
        self.margin_per_lot: float = 0.0
        self.consecutive_execution_rejections: int = 0
        self.halted: bool = False
    
    def initialize_baseline(self, equity: float) -> None:
        """Initialize baseline equity on first run."""
        if self.baseline_equity == 0.0:
            self.baseline_equity = equity
            logger.info(f"PortfolioManager: baseline_equity initialized to {equity:.2f}")
    
    def restart(self, equity: float) -> None:
        """Restart after halt - reset counters and recalculate baseline."""
        self.baseline_equity = equity
        self.consecutive_execution_rejections = 0
        self.halted = False
        self.margin_per_lot = 0.0  # Force re-cache
        logger.info(f"PortfolioManager: restarted with baseline_equity={equity:.2f}")
    
    def calculate_portfolio_pnl_fraction(self, total_profit: float) -> float:
        """Calculate portfolio PnL as fraction of baseline equity."""
        if self.baseline_equity == 0:
            logger.critical("baseline_equity == 0, halting system")
            self.halted = True
            return 0.0
        return total_profit / self.baseline_equity
    
    def can_expand(
        self,
        direction: Direction,
        long_grid: GridManager,
        short_grid: GridManager,
        equity: float,
        margin_per_lot: float,
    ) -> bool:
        """
        Check if portfolio can expand with a new position.
        Returns False if exposure limit or imbalance hard threshold is hit.
        """
        # Check if halted
        if self.halted:
            return False
        
        # Calculate total exposure
        long_volume = long_grid.total_exposure_lots()
        short_volume = short_grid.total_exposure_lots()
        total_volume = long_volume + short_volume
        
        # Check exposure limit using margin
        if margin_per_lot > 0:
            max_lots_by_margin = (equity * config.MAX_TOTAL_EXPOSURE) / margin_per_lot
            if total_volume >= max_lots_by_margin:
                logger.warning(
                    f"PortfolioManager: exposure limit hit: {total_volume:.2f} >= {max_lots_by_margin:.2f}"
                )
                return False
        else:
            # Fallback to reference lot value if margin not available
            max_lots_by_ref = (equity * config.MAX_TOTAL_EXPOSURE) / config.REFERENCE_LOT_VALUE
            if total_volume >= max_lots_by_ref:
                logger.warning(
                    f"PortfolioManager: exposure limit hit (fallback): {total_volume:.2f} >= {max_lots_by_ref:.2f}"
                )
                return False
        
        # Check imbalance hard threshold
        if total_volume > 0:
            imbalance = abs(long_volume - short_volume) / total_volume
            if imbalance >= config.IMBALANCE_HARD_THRESHOLD:
                # Check if the requested direction is the overweight side
                if direction == Direction.LONG and long_volume > short_volume:
                    logger.warning(
                        f"PortfolioManager: imbalance hard threshold hit for LONG: {imbalance:.2f}"
                    )
                    return False
                if direction == Direction.SHORT and short_volume > long_volume:
                    logger.warning(
                        f"PortfolioManager: imbalance hard threshold hit for SHORT: {imbalance:.2f}"
                    )
                    return False
        
        return True
    
    def get_add_level_threshold_adjustment(
        self,
        long_grid: GridManager,
        short_grid: GridManager,
    ) -> float:
        """
        Calculate score threshold adjustment based on imbalance.
        Returns penalty to add to ADD_LEVEL_THRESHOLD.
        """
        long_volume = long_grid.total_exposure_lots()
        short_volume = short_grid.total_exposure_lots()
        total_volume = long_volume + short_volume
        
        if total_volume == 0:
            return 0.0
        
        imbalance = abs(long_volume - short_volume) / total_volume
        
        if imbalance >= config.IMBALANCE_SOFT_THRESHOLD:
            return config.IMBALANCE_SCORE_PENALTY
        
        return 0.0
    
    def determine_degradation_mode(
        self,
        portfolio_pnl_fraction: float,
        current_atr: float,
        atr_baseline: float,
    ) -> str:
        """
        Determine current degradation mode based on portfolio PnL and ATR.
        Priority: Exit > Freeze > Conservative > Normal
        """
        # Exit mode (highest priority)
        if portfolio_pnl_fraction <= config.EXIT_MODE_DRAWDOWN_THRESHOLD:
            return DegradationMode.EXIT
        
        # Freeze mode
        if atr_baseline > 0:
            if current_atr > config.FREEZE_ATR_SPIKE_MULTIPLIER * atr_baseline:
                return DegradationMode.FREEZE
        if self.consecutive_execution_rejections >= config.FREEZE_CONSECUTIVE_REJECTIONS:
            return DegradationMode.FREEZE
        
        # Conservative mode
        if atr_baseline > 0:
            if current_atr > config.ATR_SPIKE_MULTIPLIER * atr_baseline:
                return DegradationMode.CONSERVATIVE
        if self.consecutive_execution_rejections >= config.CONSECUTIVE_REJECTIONS_FOR_CONSERVATIVE:
            return DegradationMode.CONSERVATIVE
        
        # Normal mode
        return DegradationMode.NORMAL
    
    def check_global_tp_sl(self, portfolio_pnl_fraction: float) -> Optional[str]:
        """
        Check if global TP or SL is hit.
        Returns "TP" if target hit, "SL" if stop loss hit, None otherwise.
        """
        if portfolio_pnl_fraction >= config.PORTFOLIO_TARGET:
            return "TP"
        if portfolio_pnl_fraction <= config.MAX_PORTFOLIO_DRAWDOWN:
            return "SL"
        return None
    
    def increment_execution_rejection(self) -> None:
        """Increment consecutive execution rejections counter."""
        self.consecutive_execution_rejections += 1
        logger.warning(
            f"PortfolioManager: execution rejection incremented to {self.consecutive_execution_rejections}"
        )
    
    def reset_execution_rejections(self) -> None:
        """Reset consecutive execution rejections counter."""
        if self.consecutive_execution_rejections > 0:
            logger.info(f"PortfolioManager: execution rejections reset from {self.consecutive_execution_rejections}")
        self.consecutive_execution_rejections = 0
