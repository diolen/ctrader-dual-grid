"""Parameter optimizer for backtesting - find optimal parameters for each pair."""

import logging
import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
from pathlib import Path
from datetime import datetime, timezone

from app.models.candle import Candle
from app.strategy.backtest import run_backtest_v3
from app.config.settings import config, PairConfig


logger = logging.getLogger(__name__)

# Человекочитаемые подписи для параметров оптимизации.
# Используются только в выводе/отчётах — на логику оптимизации не влияют.
PARAM_LABELS: Dict[str, str] = {
    "min_displacement_pips": "Min displacement (pips)",
    "max_displacement_pips": "Max displacement (pips)",
    "lookback_bars": "Lookback (bars)",
    "retest_bars": "Retest timeout (bars)",
    "max_retrace_percent": "Max retrace (%)",
    "retest_tolerance_pips": "Retest tolerance (pips)",
}

ENV_PARAM_MAPPING: Dict[str, str] = {
    "min_displacement_pips": "BREAKOUT_MIN_DISPLACEMENT_PIPS",
    "max_displacement_pips": "BREAKOUT_MAX_DISPLACEMENT_PIPS",
    "lookback_bars": "BREAKOUT_LEVEL_LOOKBACK_BARS",
    "retest_bars": "BREAKOUT_RETEST_TIMEOUT_BARS",
    "max_retrace_percent": "BREAKOUT_MAX_RETRACE_PERCENT",
    "retest_tolerance_pips": "BREAKOUT_RETEST_TOLERANCE_PIPS",
}


@dataclass(frozen=True)
class VolatilePairProfile:
    """Сетка оптимизации для волатильных инструментов (золото, крипто)."""
    min_displacement_range: Tuple[float, float, float]
    max_displacement_range: Tuple[float, float, float]
    lookback_range: Tuple[int, int, int]
    retest_bars_range: Tuple[int, int, int]
    max_retrace_range: Tuple[float, float, float]
    retest_tolerance_range: Tuple[float, float, float]
    min_trades: int = 5


# Диапазоны согласованы с production overrides в settings._BREAKOUT_OVERRIDES.
_VOLATILE_PAIR_PROFILES: Dict[str, VolatilePairProfile] = {
    "XAUUSD": VolatilePairProfile(
        min_displacement_range=(5.0, 40.0, 5.0),
        max_displacement_range=(500.0, 2500.0, 250.0),
        lookback_range=(50, 150, 20),
        retest_bars_range=(10, 30, 5),
        max_retrace_range=(75.0, 95.0, 5.0),
        retest_tolerance_range=(25.0, 60.0, 5.0),
    ),
    "BTCUSD": VolatilePairProfile(
        min_displacement_range=(20.0, 120.0, 20.0),
        max_displacement_range=(400.0, 1600.0, 200.0),
        lookback_range=(50, 150, 20),
        retest_bars_range=(10, 25, 5),
        max_retrace_range=(70.0, 95.0, 5.0),
        retest_tolerance_range=(30.0, 100.0, 10.0),
    ),
}


@dataclass
class BacktestResult:
    """Result of a single backtest run."""
    parameters: Dict[str, float]
    total_r: float
    winrate: float
    profit_factor: float
    trades: int
    signals: int

    @property
    def fill_rate(self) -> float:
        """Доля сигналов, превратившихся в исполненные трейды."""
        if self.signals <= 0:
            return 0.0
        return self.trades / self.signals


@dataclass
class ParameterRange:
    """Range for a parameter to optimize."""
    name: str
    min_val: float
    max_val: float
    step: float


@dataclass
class OptimizationConfig:
    """Configuration for parameter optimization."""
    pair: str
    candles: List[Candle]
    digits: int
    pip_value: float
    timeframe: str = "M5"
    trail_pips: float = 5.0
    
    # Parameter ranges to optimize
    min_displacement_range: Tuple[float, float, float] = (2.0, 10.0, 1.0)  # min, max, step
    max_displacement_range: Tuple[float, float, float] = (20.0, 50.0, 5.0)
    lookback_range: Tuple[int, int, int] = (50, 150, 10)
    retest_bars_range: Tuple[int, int, int] = (10, 25, 5)
    max_retrace_range: Optional[Tuple[float, float, float]] = None
    retest_tolerance_range: Optional[Tuple[float, float, float]] = None
    
    # Optimization constraints
    min_trades: int = 10  # Minimum trades to consider result valid
    min_winrate: float = 0.0  # Removed winrate constraint
    max_iterations: int = 100


def _float_range(spec: Tuple[float, float, float]) -> List[float]:
    lo, hi, step = spec
    n = int((hi - lo) / step) + 1
    return [lo + i * step for i in range(n)]


def _int_range(spec: Tuple[int, int, int]) -> List[int]:
    lo, hi, step = spec
    return list(range(lo, hi + 1, step))


def _median_price(candles: List[Candle]) -> float:
    closes = sorted(c.close for c in candles)
    return closes[len(closes) // 2]


def _validate_pip_value(pair: str, candles: List[Candle], pip_value: float) -> None:
    """Предупреждение, если pip_value не соответствует масштабу цены инструмента."""
    median = _median_price(candles)
    if pair == "BTCUSD" and median > 1000 and pip_value < 0.1:
        logger.warning(
            f"⚠️ [{pair}] pip_value={pip_value} слишком мал для цены ~{median:.0f}. "
            "Используйте --pip-value 1.0 (при 0.01 почти все сетапы отменяются "
            "по displacement_too_large)."
        )
    elif pair == "XAUUSD" and median > 100 and pip_value < 0.001:
        logger.warning(
            f"⚠️ [{pair}] pip_value={pip_value} подозрительно мал для цены ~{median:.1f}. "
            "Для золота обычно --pip-value 0.01 --digits 2."
        )


def _apply_volatile_pair_profile(opt_config: OptimizationConfig) -> None:
    """Подставляет расширенную сетку для XAUUSD / BTCUSD."""
    profile = _VOLATILE_PAIR_PROFILES.get(opt_config.pair.upper())
    if not profile:
        return
    opt_config.min_displacement_range = profile.min_displacement_range
    opt_config.max_displacement_range = profile.max_displacement_range
    opt_config.lookback_range = profile.lookback_range
    opt_config.retest_bars_range = profile.retest_bars_range
    opt_config.max_retrace_range = profile.max_retrace_range
    opt_config.retest_tolerance_range = profile.retest_tolerance_range
    opt_config.min_trades = profile.min_trades


def _pair_config_from_params(
    base: PairConfig,
    parameters: Dict[str, float],
    *,
    timeframe: str,
    trail_pips: float,
) -> PairConfig:
    """Собирает PairConfig с переопределёнными параметрами оптимизации."""
    return PairConfig(
        pair=base.pair,
        entry_timeframe=timeframe,
        risk_pct=base.risk_pct,
        max_spread_pips=base.max_spread_pips,
        level_lookback_bars=int(parameters["lookback_bars"]),
        min_touches=base.min_touches,
        touch_tolerance_pips=base.touch_tolerance_pips,
        level_cluster_pips=base.level_cluster_pips,
        min_level_age_bars=base.min_level_age_bars,
        max_level_age_bars=base.max_level_age_bars,
        breakout_close_buffer_pips=base.breakout_close_buffer_pips,
        min_body_pips=base.min_body_pips,
        max_bars_since_touch=base.max_bars_since_touch,
        min_displacement_pips=parameters["min_displacement_pips"],
        max_displacement_pips=parameters["max_displacement_pips"],
        retest_tolerance_pips=parameters.get(
            "retest_tolerance_pips", base.retest_tolerance_pips,
        ),
        retest_timeout_bars=int(parameters["retest_bars"]),
        sl_buffer_pips=base.sl_buffer_pips,
        max_bars_for_displacement=base.max_bars_for_displacement,
        min_volume_mult=base.min_volume_mult,
        volume_lookback_bars=base.volume_lookback_bars,
        min_retrace_percent=base.min_retrace_percent,
        max_retrace_percent=parameters.get(
            "max_retrace_percent", base.max_retrace_percent,
        ),
        v3_trail_pips=trail_pips,
    )


class ParameterOptimizer:
    """Optimize backtest parameters using grid search."""
    
    def __init__(self, config: OptimizationConfig):
        self.config = config
        self.results: List[BacktestResult] = []
        # Статистика прогона — только для отчётности, не влияет на логику оптимизации.
        self._total_combinations: int = 0
        self._rejected_count: int = 0
        self._error_count: int = 0
    
    def generate_parameter_combinations(self) -> List[Dict[str, float]]:
        """Generate all parameter combinations to test."""
        combinations: List[Dict[str, float]] = []

        max_retrace_values = (
            _float_range(self.config.max_retrace_range)
            if self.config.max_retrace_range
            else [None]
        )
        retest_tol_values = (
            _float_range(self.config.retest_tolerance_range)
            if self.config.retest_tolerance_range
            else [None]
        )

        for min_disp in _float_range(self.config.min_displacement_range):
            for max_disp in _float_range(self.config.max_displacement_range):
                if max_disp <= min_disp:
                    continue
                for lookback in _int_range(self.config.lookback_range):
                    for retest_bars in _int_range(self.config.retest_bars_range):
                        for max_retrace in max_retrace_values:
                            for retest_tol in retest_tol_values:
                                combo: Dict[str, float] = {
                                    "min_displacement_pips": min_disp,
                                    "max_displacement_pips": max_disp,
                                    "lookback_bars": float(lookback),
                                    "retest_bars": float(retest_bars),
                                }
                                if max_retrace is not None:
                                    combo["max_retrace_percent"] = max_retrace
                                if retest_tol is not None:
                                    combo["retest_tolerance_pips"] = retest_tol
                                combinations.append(combo)
        
        # Limit combinations to max_iterations using uniform stride sampling
        # (not a head-slice): a plain combinations[:max_iterations] only ever
        # advances the outermost loop variables, so any limited run collapses
        # onto a single value for whichever parameters sit in the outer loops
        # (lookback/retest here) while the inner ones (displacement) vary freely
        # -- or vice versa if the loop order is flipped. Sampling at a uniform
        # stride across the full ordered list keeps all four parameters
        # varying together regardless of max_iterations or loop order.
        if len(combinations) > self.config.max_iterations:
            n = self.config.max_iterations
            stride = len(combinations) / n
            combinations = [combinations[int(i * stride)] for i in range(n)]
        
        logger.info(f"Generated {len(combinations)} parameter combinations")
        return combinations
    
    async def run_single_backtest(self, parameters: Dict[str, float]) -> Optional[BacktestResult]:
        """Run a single backtest with given parameters."""
        try:
            pair_cfg = config.get_pair_config(self.config.pair)
            pair_cfg = _pair_config_from_params(
                pair_cfg,
                parameters,
                timeframe=self.config.timeframe,
                trail_pips=self.config.trail_pips,
            )
            
            result_data = await run_backtest_v3(
                self.config.candles,
                self.config.digits,
                timeframe=self.config.timeframe,
                pair=self.config.pair,
                pip_value=self.config.pip_value,
                pair_config=pair_cfg,
                trail_pips=self.config.trail_pips,
                silent=True,
            )
            
            result = BacktestResult(
                parameters=parameters,
                total_r=result_data["total_r"],
                winrate=result_data["winrate"] / 100,
                profit_factor=result_data["profit_factor"] or 0.0,
                trades=result_data["total"],
                signals=result_data["signals_generated"],
            )
            
            if result.trades >= self.config.min_trades:
                return result

            self._rejected_count += 1
            return None
            
        except Exception:
            self._error_count += 1
            return None
    
    def _parse_backtest_output(self, output: str, parameters: Dict[str, float]) -> Optional[BacktestResult]:
        """Parse backtest output to extract results."""
        try:
            lines = output.split('\n')
            result_dict = {}
            
            for line in lines:
                if "Итого R" in line:
                    # Extract R value (e.g., "Итого R                 : +51.7R")
                    parts = line.split(':')
                    if len(parts) > 1:
                        r_str = parts[1].strip().replace('R', '').replace('+', '')
                        result_dict['total_r'] = float(r_str)
                elif "Winrate" in line:
                    parts = line.split(':')
                    if len(parts) > 1:
                        winrate_str = parts[1].strip().replace('%', '')
                        result_dict['winrate'] = float(winrate_str) / 100
                elif "Profit factor" in line:
                    parts = line.split(':')
                    if len(parts) > 1:
                        pf_str = parts[1].strip()
                        result_dict['profit_factor'] = float(pf_str)
                elif "Сигналов сгенерировано" in line:
                    parts = line.split(':')
                    if len(parts) > 1:
                        signals_str = parts[1].strip()
                        result_dict['signals'] = int(signals_str)
                elif "Сделок (filled)" in line:
                    parts = line.split(':')
                    if len(parts) > 1:
                        trades_str = parts[1].strip()
                        result_dict['trades'] = int(trades_str)
            
            if 'total_r' in result_dict:
                return BacktestResult(
                    parameters=parameters,
                    total_r=result_dict.get('total_r', 0.0),
                    winrate=result_dict.get('winrate', 0.0),
                    profit_factor=result_dict.get('profit_factor', 0.0),
                    trades=result_dict.get('trades', 0),
                    signals=result_dict.get('signals', 0),
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error parsing backtest output: {e}")
            return None
    
    async def optimize(self) -> List[BacktestResult]:
        """Run optimization over all parameter combinations."""
        combinations = self.generate_parameter_combinations()
        self._total_combinations = len(combinations)
        self._rejected_count = 0
        self._error_count = 0
        results = []
        
        logger.info(f"Starting optimization for {self.config.pair} ({len(combinations)} combinations)...")
        
        for params in combinations:
            result = await self.run_single_backtest(params)
            if result:
                results.append(result)
        
        # Sort by total R (descending)
        results.sort(key=lambda x: x.total_r, reverse=True)
        
        self.results = results
        if not results:
            await self.log_zero_results_diagnostic()
        return results

    async def log_zero_results_diagnostic(self) -> None:
        """Диагностика filter_stats при 0 валидных результатов."""
        from app.strategy.breakout_retest_v3 import BreakoutRetestScalpingV3Strategy
        from app.strategy.trade_guard import TradeGuard
        from app.strategy.base import MarketData

        pair = self.config.pair
        pair_cfg = config.get_pair_config(pair)
        candles = self.config.candles
        pip_value = self.config.pip_value

        logger.warning("=" * 70)
        logger.warning(f"🔍 ДИАГНОСТИКА [{pair}]: 0 валидных результатов оптимизации")
        logger.warning(
            f"  Свечей: {len(candles)} | pip_value: {pip_value} | "
            f"median price: {_median_price(candles):.2f}"
        )
        logger.warning(
            f"  Production пороги: disp={pair_cfg.min_displacement_pips}-"
            f"{pair_cfg.max_displacement_pips}p | "
            f"retrace={pair_cfg.min_retrace_percent:.0f}-"
            f"{pair_cfg.max_retrace_percent:.0f}% | "
            f"retest_tol={pair_cfg.retest_tolerance_pips}p | "
            f"retest_timeout={pair_cfg.retest_timeout_bars}bars"
        )

        _validate_pip_value(pair, candles, pip_value)

        trade_guard = TradeGuard()
        strategy = BreakoutRetestScalpingV3Strategy(
            trade_guard=trade_guard,
            pair_config=pair_cfg,
            pip_value=pip_value,
        )
        warmup = max(
            config.BACKTEST_WARMUP_BARS,
            pair_cfg.level_lookback_bars + 50,
        )
        if hasattr(strategy, "cold_start"):
            strategy.cold_start(candles[:warmup], pair)

        spread = config.BACKTEST_ASSUMED_SPREAD_PIPS * pip_value
        for i in range(warmup, len(candles)):
            await strategy.update(
                MarketData(
                    pair=pair,
                    candles=candles[: i + 1],
                    is_warmup=False,
                    spread=spread if spread > 0 else None,
                )
            )

        fs = strategy.filter_stats()
        prod = await run_backtest_v3(
            candles,
            self.config.digits,
            timeframe=self.config.timeframe,
            pair=pair,
            pip_value=pip_value,
            pair_config=pair_cfg,
            trail_pips=self.config.trail_pips,
            silent=True,
        )
        logger.warning(
            f"  Бэктест (production cfg): signals={prod['signals_generated']} "
            f"trades={prod['total']}"
        )
        logger.warning(f"  Торговое окно: {fs['trade_window']}")
        logger.warning(f"  Баров в окне: {fs['ticks_in_window']}")
        reject_keys = (
            "window_fail", "no_level", "displacement_too_large", "slow_displacement",
            "no_displacement", "retest_timeout", "shallow_retest_cancelled",
            "guard_block", "spread_too_high",
        )
        for key in reject_keys:
            if fs.get(key, 0) > 0:
                logger.warning(f"  {key}: {fs[key]}")
        if pair in _VOLATILE_PAIR_PROFILES:
            profile = _VOLATILE_PAIR_PROFILES[pair]
            logger.warning(
                f"  Сетка оптимизатора: disp "
                f"{profile.min_displacement_range[0]}-"
                f"{profile.max_displacement_range[1]}p | "
                f"max_retrace до {profile.max_retrace_range[1]}% | "
                f"min_trades={profile.min_trades}"
            )
        logger.warning("=" * 70)
    
    def get_best_parameters(self, top_n: int = 5) -> List[BacktestResult]:
        """Get top N parameter combinations."""
        return self.results[:top_n]

    @staticmethod
    def _format_param_value(value: float) -> str:
        """Аккуратное форматирование числовых параметров (без хвостов float-погрешности)."""
        rounded = round(float(value), 4)
        if rounded == int(rounded):
            return str(int(rounded))
        return f"{rounded:g}"

    @staticmethod
    def _format_profit_factor(pf: float) -> str:
        if pf == float("inf"):
            return "inf"
        return f"{pf:.2f}"

    def _format_parameters_line(self, parameters: Dict[str, float], keys: Optional[List[str]] = None) -> str:
        """Человекочитаемая строка параметров вместо сырого dict.

        Если передан `keys`, выводятся только эти параметры (используется,
        чтобы не повторять в каждой строке значения, одинаковые для всего топ-N).
        """
        items = parameters.items() if keys is None else [(k, parameters[k]) for k in keys if k in parameters]
        parts = []
        for key, value in items:
            label = PARAM_LABELS.get(key, key)
            parts.append(f"{label}={self._format_param_value(value)}")
        return " | ".join(parts) if parts else "(same as above)"

    @staticmethod
    def _split_varying_constant_params(
        results: List[BacktestResult],
    ) -> Tuple[Dict[str, float], List[str]]:
        """Разделяет параметры топ-N на константы (одинаковы во всех результатах)
        и варьирующиеся — чтобы в выводе показывать только то, что реально меняется."""
        if not results:
            return {}, []

        all_keys = list(results[0].parameters.keys())
        constants: Dict[str, float] = {}
        varying: List[str] = []

        for key in all_keys:
            values = {r.parameters.get(key) for r in results}
            if len(values) == 1:
                constants[key] = results[0].parameters[key]
            else:
                varying.append(key)

        return constants, varying

    def summary_dict(self, top_n: int = 10) -> Dict:
        """Структурированная сводка по прогону — пригодна для экспорта (JSON/CSV/отчёты)."""
        top_results = self.get_best_parameters(top_n)
        return {
            "pair": self.config.pair,
            "timeframe": self.config.timeframe,
            "trail_pips": self.config.trail_pips,
            "total_combinations_tested": self._total_combinations,
            "valid_results": len(self.results),
            "rejected_min_trades": self._rejected_count,
            "errors": self._error_count,
            "min_trades_threshold": self.config.min_trades,
            "top_results": [
                {
                    "rank": i,
                    "total_r": r.total_r,
                    "winrate": r.winrate,
                    "profit_factor": r.profit_factor,
                    "trades": r.trades,
                    "signals": r.signals,
                    "fill_rate": r.fill_rate,
                    "parameters": r.parameters,
                }
                for i, r in enumerate(top_results, 1)
            ],
        }
    
    def print_results(self, top_n: int = 10) -> None:
        """Print optimization results."""
        logger.info("=" * 84)
        logger.info(f"OPTIMIZATION RESULTS FOR {self.config.pair}")
        logger.info("=" * 84)

        # Сводка по прогону — сколько комбинаций протестировано и сколько отсеяно.
        logger.info(
            f"Tested: {self._total_combinations} combinations │ "
            f"Valid: {len(self.results)} │ "
            f"Rejected (min_trades<{self.config.min_trades}): {self._rejected_count} │ "
            f"Errors: {self._error_count}"
        )

        if not self.results:
            logger.warning("No valid results found. All backtests either failed or had insufficient trades.")
            logger.info(f"Minimum trades required: {self.config.min_trades}")
            logger.info("=" * 84)
            return
        top_results = self.get_best_parameters(top_n)

        # Параметры, одинаковые для всего топ-N, выносим в шапку — чтобы не повторять
        # их в каждой строке (особенно заметно, когда сетка перебора была узкой).
        constants, varying_keys = self._split_varying_constant_params(top_results)
        if constants:
            logger.info(f"Fixed across top-{len(top_results)}: {self._format_parameters_line(constants)}")

        logger.info("-" * 84)
        header = (
            f"{'#':<4}│{'Total R':>9}│{'Winrate':>9}│{'PF':>7}│"
            f"{'Trades':>7}│{'Signals':>8}│{'Fill%':>7}"
        )
        logger.info(header)
        logger.info("-" * 84)

        for i, result in enumerate(top_results, 1):
            marker = "★" if i == 1 else " "
            pf_str = self._format_profit_factor(result.profit_factor)
            logger.info(
                f"{marker}{i:<3}│{result.total_r:>+8.1f}R│{result.winrate:>8.1%}│{pf_str:>7}│"
                f"{result.trades:>7}│{result.signals:>8}│{result.fill_rate:>6.1%}"
            )
            params_line = self._format_parameters_line(result.parameters, keys=varying_keys or None)
            logger.info(f"     {params_line}")

        logger.info("-" * 84)
        logger.info(f"Best: #1 -> Total R: {top_results[0].total_r:+.1f}R "
                    f"(Winrate {top_results[0].winrate:.1%}, PF {self._format_profit_factor(top_results[0].profit_factor)})")
        logger.info("=" * 84)


async def optimize_pair_parameters(
    pair: str,
    csv_file: str,
    digits: int,
    pip_value: float,
    timeframe: str = "M5",
    trail_pips: float = 5.0,
    max_iterations: int = 50,
) -> None:
    """
    Optimize parameters for a specific pair.
    
    Args:
        pair: Trading pair symbol
        csv_file: Path to CSV file with candle data
        digits: Number of decimal places
        pip_value: Pip value
        timeframe: Timeframe
        trail_pips: Trailing stop in pips
        max_iterations: Maximum number of parameter combinations to test
    """
    from app.backtest.local_backtest import load_candles_from_csv
    
    # Load candles
    candles = load_candles_from_csv(csv_file)
    logger.info(f"Loaded {len(candles)} candles for optimization")

    pair = pair.upper()
    pair_cfg = config.get_pair_config(pair)
    if trail_pips == 5.0:
        trail_pips = pair_cfg.v3_trail_pips

    _validate_pip_value(pair, candles, pip_value)
    
    # Create optimization config
    opt_config = OptimizationConfig(
        pair=pair,
        candles=candles,
        digits=digits,
        pip_value=pip_value,
        timeframe=timeframe,
        trail_pips=trail_pips,
        max_iterations=max_iterations,
    )
    
    _apply_volatile_pair_profile(opt_config)
    
    # Run optimization
    optimizer = ParameterOptimizer(opt_config)
    results = await optimizer.optimize()
    
    # Print results
    optimizer.print_results()
    
    # Save best parameters to file
    if results:
        best = results[0]
        top_results = optimizer.get_best_parameters(5)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        output_file = Path(f"data/{pair}_optimized_params.txt")
        with open(output_file, 'w') as f:
            f.write(f"# Optimized parameters for {pair}\n")
            f.write(f"# Generated: {timestamp}\n")
            f.write(f"# Timeframe: {timeframe} | Trail: {trail_pips} pips\n")
            f.write(
                f"# Tested {optimizer._total_combinations} combinations | "
                f"{len(results)} valid (min_trades>={opt_config.min_trades})\n"
            )
            f.write(f"# Best -> Total R: {best.total_r:+.1f}R | Winrate: {best.winrate:.1%} | "
                    f"PF: {optimizer._format_profit_factor(best.profit_factor)} | Trades: {best.trades}\n")
            f.write("#\n")

            # Map optimizer parameter names to .env format, prefixed with the pair
            # so params for different pairs (e.g. EURUSD vs XAUUSD) don't collide
            # under the same env var name when merged into a shared .env file.
            for key, value in best.parameters.items():
                env_key = ENV_PARAM_MAPPING.get(key, key.upper())
                f.write(f"{pair}_{env_key}={value}\n")

            # Альтернативные топ-результаты сохраняются как справка (закомментированы),
            # чтобы можно было быстро сравнить варианты без повторного запуска оптимизации.
            if len(top_results) > 1:
                f.write("#\n# Alternative top results (for reference):\n")
                for i, r in enumerate(top_results[1:], 2):
                    f.write(f"# --- #{i} -> Total R: {r.total_r:+.1f}R | Winrate: {r.winrate:.1%} | "
                            f"PF: {optimizer._format_profit_factor(r.profit_factor)} | Trades: {r.trades} ---\n")
                    for key, value in r.parameters.items():
                        env_key = ENV_PARAM_MAPPING.get(key, key.upper())
                        f.write(f"# {pair}_{env_key}={value}\n")
        
        logger.info(f"Best parameters saved to {output_file}")