"""Portfolio Manager for Trading Layer - portfolio-level risk management."""

import logging
from typing import Dict, Optional

from app.trading.grid_models import Direction, DegradationMode
from app.trading.grid_book import GridBook, PairGrids
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
        self.margin_per_lot = 0.0
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
        pair: str,
        direction: Direction,
        pair_grids: PairGrids,
        grid_book: GridBook,
        equity: float,
        margin_per_lot: float,
        additional_lots: float,
        margin_by_pair: Dict[str, float],
    ) -> bool:
        """
        Check if a new level can be opened for ``pair`` in ``direction``.

        Checks (in order): halted, pair imbalance hard, pair exposure cap,
        global exposure cap.
        """
        if self.halted:
            return False

        if margin_per_lot <= 0:
            logger.warning(
                f"PortfolioManager: margin_per_lot not cached for {pair}, blocking expand",
            )
            return False

        pair_long = pair_grids.pair_long_lots()
        pair_short = pair_grids.pair_short_lots()
        pair_total = pair_long + pair_short

        if pair_total > 0:
            pair_imbalance = abs(pair_long - pair_short) / pair_total
            if pair_imbalance >= config.IMBALANCE_HARD_THRESHOLD:
                if direction == Direction.LONG and pair_long > pair_short:
                    logger.warning(
                        f"PortfolioManager: [{pair}] imbalance {pair_imbalance:.0%} "
                        f"(L={pair_long:.4f} S={pair_short:.4f}) blocks LONG",
                    )
                    return False
                if direction == Direction.SHORT and pair_short > pair_long:
                    logger.warning(
                        f"PortfolioManager: [{pair}] imbalance {pair_imbalance:.0%} "
                        f"(L={pair_long:.4f} S={pair_short:.4f}) blocks SHORT",
                    )
                    return False

        projected_pair_lots = pair_total + additional_lots
        max_pair_lots = (equity * config.MAX_PAIR_EXPOSURE) / margin_per_lot
        if projected_pair_lots > max_pair_lots:
            logger.warning(
                f"PortfolioManager: [{pair}] pair exposure limit: "
                f"{projected_pair_lots:.4f} > {max_pair_lots:.4f} lots",
            )
            return False

        projected_global_margin = (
            grid_book.estimated_total_margin(margin_by_pair)
            + additional_lots * margin_per_lot
        )
        max_global_margin = equity * config.MAX_TOTAL_EXPOSURE
        if projected_global_margin > max_global_margin:
            logger.warning(
                f"PortfolioManager: global exposure limit for {pair}: "
                f"margin {projected_global_margin:.2f} > {max_global_margin:.2f}",
            )
            return False

        return True

    def get_add_level_threshold_adjustment(self, pair_grids: PairGrids) -> float:
        """Score penalty when pair long/short imbalance exceeds soft threshold."""
        pair_long = pair_grids.pair_long_lots()
        pair_short = pair_grids.pair_short_lots()
        pair_total = pair_long + pair_short

        if pair_total == 0:
            return 0.0

        imbalance = abs(pair_long - pair_short) / pair_total
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
        if portfolio_pnl_fraction <= config.EXIT_MODE_DRAWDOWN_THRESHOLD:
            return DegradationMode.EXIT

        if atr_baseline > 0:
            if current_atr > config.FREEZE_ATR_SPIKE_MULTIPLIER * atr_baseline:
                return DegradationMode.FREEZE
        if self.consecutive_execution_rejections >= config.FREEZE_CONSECUTIVE_REJECTIONS:
            return DegradationMode.FREEZE

        if atr_baseline > 0:
            if current_atr > config.ATR_SPIKE_MULTIPLIER * atr_baseline:
                return DegradationMode.CONSERVATIVE
        if self.consecutive_execution_rejections >= config.CONSECUTIVE_REJECTIONS_FOR_CONSERVATIVE:
            return DegradationMode.CONSERVATIVE

        return DegradationMode.NORMAL

    def check_global_tp_sl(self, portfolio_pnl_fraction: float) -> Optional[str]:
        """Returns \"TP\", \"SL\", or None."""
        if portfolio_pnl_fraction >= config.PORTFOLIO_TARGET:
            return "TP"
        if portfolio_pnl_fraction <= config.MAX_PORTFOLIO_DRAWDOWN:
            return "SL"
        return None

    def increment_execution_rejection(self) -> None:
        self.consecutive_execution_rejections += 1
        logger.warning(
            f"PortfolioManager: execution rejection incremented to "
            f"{self.consecutive_execution_rejections}",
        )

    def reset_execution_rejections(self) -> None:
        if self.consecutive_execution_rejections > 0:
            logger.info(
                f"PortfolioManager: execution rejections reset from "
                f"{self.consecutive_execution_rejections}",
            )
        self.consecutive_execution_rejections = 0
