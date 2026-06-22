import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

from app.models.candle import Candle
from app.strategy.orchestrator import StrategyOrchestrator
from app.strategy.base import MarketData
from app.models.signal import Signal as UnifiedSignal
from app.config.settings import config


def _candle_ts_ms(candle: Candle) -> int:
    ts = candle.timestamp
    if hasattr(ts, "timestamp"):
        return int(ts.timestamp() * 1000)
    return int(ts)


def _stop_filled(signal: UnifiedSignal, candle: Candle) -> bool:
    """Stop order: BUY — high >= stop, SELL — low <= stop."""
    if signal.direction == "BUY":
        return candle.high >= signal.entry
    return candle.low <= signal.entry


@dataclass
class _TrailState:
    peak: float
    trough: float
    effective_sl: float


def _bar_exit_v3_trailing(
    signal: UnifiedSignal,
    candle: Candle,
    trail: _TrailState,
    trail_pips: float,
    pip_value: float,
) -> Optional[Tuple[str, float]]:
    """
    Выход v3: SL + trailing (без TP). При одновременном касании — консервативно SL.
    """
    risk = abs(signal.entry - signal.stop_loss)
    if risk <= 0:
        return None
    trail_dist = trail_pips * pip_value

    if signal.direction == "BUY":
        trail.peak = max(trail.peak, candle.high)
        if trail.peak - signal.entry >= trail_dist:
            trail.effective_sl = max(trail.effective_sl, trail.peak - trail_dist)
        hit_sl = candle.low <= trail.effective_sl
        if hit_sl:
            r_delta = (trail.effective_sl - signal.entry) / risk
            return ("WIN" if r_delta > 0 else "LOSS", r_delta)
    else:
        trail.trough = min(trail.trough, candle.low)
        if signal.entry - trail.trough >= trail_dist:
            trail.effective_sl = min(trail.effective_sl, trail.trough + trail_dist)
        hit_sl = candle.high >= trail.effective_sl
        if hit_sl:
            r_delta = (signal.entry - trail.effective_sl) / risk
            return ("WIN" if r_delta > 0 else "LOSS", r_delta)
    return None


def _max_spread_pips(pair_config: object) -> float:
    if pair_config and hasattr(pair_config, "max_spread_pips"):
        return float(pair_config.max_spread_pips)
    raise ValueError("run_backtest: pair_config обязателен")


def _fill_timeout_bars(bar_minutes: int = 5) -> int:
    """Сколько свечей ждём исполнения limit (PENDING_ORDER_MAX_AGE_SECONDS)."""
    bar_sec = bar_minutes * 60
    return max(1, int(config.PENDING_ORDER_MAX_AGE_SECONDS / bar_sec))


def _profit_factor(r_deltas: List[float]) -> Optional[float]:
    """Gross profit R / gross loss R. None если нет закрытых сделок с убытком."""
    gross_profit = sum(r for r in r_deltas if r > 0)
    gross_loss = abs(sum(r for r in r_deltas if r < 0))
    if gross_loss == 0:
        return None if gross_profit == 0 else float("inf")
    return gross_profit / gross_loss


def _max_drawdown_r(r_deltas: List[float]) -> float:
    """Максимальная просадка кривой накопленного R (peak-to-trough)."""
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for r in r_deltas:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return max_dd


_BACKTEST_QUIET_LOGGERS = (
    "app.strategy.breakout_retest_v3",
    "app.strategy.trade_guard",
    "app.strategy.orchestrator",
)


@contextmanager
def _backtest_log_levels(quiet: bool) -> Iterator[None]:
    """В бэктесте при quiet — только WARNING+ для шумных модулей стратегии."""
    if not quiet:
        yield
        return
    saved: Dict[str, int] = {}
    for name in _BACKTEST_QUIET_LOGGERS:
        log = logging.getLogger(name)
        saved[name] = log.level
        log.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def _format_profit_factor(pf: Optional[float]) -> str:
    if pf is None:
        return "—"
    if pf == float("inf"):
        return "∞ (нет убытков)"
    return f"{pf:.2f}"


def _log_zero_signals_diagnostic(pair: str, pair_config: object, strategy) -> None:
    """Подсказка, почему бэктест не дал ни одного сигнала."""
    pc = pair_config
    logging.info("-" * 70)
    logging.info(f"🔍 [{pair}] Диагностика: 0 сигналов за период")
    logging.info(
        f"  Пороги: min_touches={pc.min_touches} "
        f"disp={pc.min_displacement_pips}-{pc.max_displacement_pips}p "
        f"retest_timeout={pc.retest_timeout_bars}bars "
        f"max_spread={pc.max_spread_pips}p"
    )
    fs = strategy.filter_stats()
    logging.info(f"  Торговое окно (UTC)     : {fs['trade_window']}")
    tw = fs["ticks_in_window"]
    logging.info(f"  Баров в окне (ticks)    : {tw}")
    logging.info(f"  Сигналов                : {fs.get('signals', 0)}")
    logging.info(
        f"  Отсевы: вне окна={fs['window_fail']} no_level={fs.get('no_level', 0)} "
        f"disp_large={fs.get('displacement_too_large', 0)} "
        f"no_disp={fs.get('no_displacement', 0)} "
        f"retest_timeout={fs.get('retest_timeout', 0)} "
        f"guard={fs.get('guard_block', 0)}"
    )
    if fs["window_fail"] > 0 and fs["ticks_in_window"] == 0:
        logging.warning(
            "  ⚠️ Все бары вне TRADE_WINDOW — проверьте TRADE_WINDOW_START/END в .env"
        )
    if tw > 0 and fs.get("guard_block", 0) >= tw * 0.5:
        logging.warning(
            "  ⚠️ TradeGuard блокирует ≥50% баров в окне — проверьте зависшие pending/позиции"
        )
    logging.info("-" * 70)


def _print_results(r: dict, pair: Optional[str] = None) -> None:
    profitable = "✅ ПРИБЫЛЬНО" if r["profitable"] else "❌ УБЫТОЧНО"
    pair_label = f"{pair} " if pair else ""
    logging.info("=" * 70)
    logging.info(
        f"📊 РЕЗУЛЬТАТЫ БЭКТЕСТА (live-mode) ({pair_label}{r['bars']} свечей {r['timeframe']})"
    )
    logging.info("=" * 70)
    logging.info(f"  Режим                   : 1 позиция/пара, limit fill, spread filter")
    logging.info(f"  Прогрев                 : {r.get('warmup_bars', 0)} свечей")
    logging.info(f"  Spread (assumed)        : {r.get('assumed_spread_pips', 0):.1f} pips")
    logging.info(f"  Сигналов сгенерировано  : {r.get('signals_generated', r['total'])}")
    logging.info(f"  Отклонено (spread)      : {r.get('rejected_spread', 0)}")
    logging.info(f"  Истекло (нет fill)      : {r.get('expired_no_fill', 0)}")
    logging.info(f"  Сделок (filled)         : {r['total']}")
    if r["avg_rr"] > 0:
        logging.info(f"  Средний RR (TP/SL)       : 1:{r['avg_rr']:.2f}")
    logging.info(f"  LONG / SHORT            : {r['longs']} / {r['shorts']}")
    logging.info(f"  Побед  (WIN)            : {r['wins']}")
    logging.info(f"  Потерь (LOSS)           : {r['losses']}")
    logging.info(f"  Без результата          : {r['no_result']}")
    if r["wins"] + r["losses"] > 0:
        logging.info(f"  Winrate                 : {r['winrate']:.1f}%")
        logging.info(f"  Безубыточный winrate    : {r['breakeven']:.1f}%")
        logging.info(f"  {profitable}")
        logging.info(f"  Итого R                 : {r['total_r']:+.1f}R")
        logging.info(f"  Средний R на сделку     : {r['avg_r']:+.2f}R")
        logging.info(f"  Profit factor           : {_format_profit_factor(r.get('profit_factor'))}")
        logging.info(f"  Max drawdown            : {r.get('max_drawdown_r', 0):.1f}R")
    else:
        logging.info("  Winrate                 : — (нет завершённых сделок)")
    logging.info("=" * 70)


@dataclass
class _ActiveTradeV3:
    signal: UnifiedSignal
    signal_bar_idx: int
    placed_bar_idx: int
    order_id: str
    trail: _TrailState
    filled: bool = False
    fill_bar_idx: Optional[int] = None


async def run_backtest_v3(
    candles: List[Candle],
    digits: int,
    timeframe: Optional[str] = None,
    pair: Optional[str] = None,
    silent: bool = False,
    pip_value: float = 0.0001,
    pair_config: object = None,
    trail_pips: float = 8.0,
) -> dict:
    """
    Бэктест Breakout Retest v3: stop fill, trailing SL, без TP.
    """
    from app.strategy.breakout_retest_v3 import BreakoutRetestScalpingV3Strategy
    from app.strategy.trade_guard import TradeGuard

    if not pair_config:
        raise ValueError("run_backtest_v3: pair_config обязателен")
    tf = timeframe or pair_config.entry_timeframe
    pair_label = f"{pair} " if pair else ""
    pair_key = pair or "UNKNOWN"
    bar_minutes = 5 if tf == "M5" else (1 if tf == "M1" else 5)

    quiet_logs = config.BACKTEST_QUIET_LOGS
    warmup_bars = max(
        config.BACKTEST_WARMUP_BARS,
        pair_config.level_lookback_bars + 50,
    )
    assumed_spread = config.BACKTEST_ASSUMED_SPREAD_PIPS
    max_spread = _max_spread_pips(pair_config)
    fill_timeout = _fill_timeout_bars(bar_minutes)

    trade_guard = TradeGuard()
    strategy = BreakoutRetestScalpingV3Strategy(
        trade_guard=trade_guard,
        pair_config=pair_config,
        pip_value=pip_value,
    )

    open_trade: Optional[_ActiveTradeV3] = None
    completed: List[Tuple[UnifiedSignal, str, float]] = []
    stats = {
        "signals_generated": 0,
        "rejected_spread": 0,
        "expired_no_fill": 0,
        "trades_filled": 0,
    }

    with _backtest_log_levels(quiet_logs):
        if hasattr(strategy, "cold_start") and pair:
            strategy.cold_start(candles[:warmup_bars], pair)

        for i in range(len(candles)):
            candle = candles[i]

            if open_trade is not None:
                trade = open_trade

                if not trade.filled:
                    if i > trade.placed_bar_idx:
                        if _stop_filled(trade.signal, candle):
                            trade.filled = True
                            trade.fill_bar_idx = i
                            stats["trades_filled"] += 1
                            strategy.on_order_filled(pair_key)
                            await strategy.release_signal_block(pair_key)
                        elif i - trade.placed_bar_idx >= fill_timeout:
                            stats["expired_no_fill"] += 1
                            await strategy.release_signal_block(pair_key)
                            open_trade = None
                    continue

                if trade.fill_bar_idx is not None and i > trade.fill_bar_idx:
                    exit_res = _bar_exit_v3_trailing(
                        trade.signal,
                        candle,
                        trade.trail,
                        trail_pips,
                        pip_value,
                    )
                    if exit_res:
                        result, r_delta = exit_res
                        completed.append((trade.signal, result, r_delta))
                        await strategy.release_signal_block(pair_key)
                        open_trade = None
                continue

            spread_price = (
                assumed_spread * pip_value if assumed_spread > 0 else None
            )
            market_data = MarketData(
                pair=pair_key,
                candles=candles[: i + 1],
                is_warmup=i < warmup_bars,
                spread=spread_price,
            )
            signal = await strategy.update(market_data)
            if signal is None:
                continue

            stats["signals_generated"] += 1

            if assumed_spread > 0 and assumed_spread > max_spread:
                stats["rejected_spread"] += 1
                await strategy.release_signal_block(pair_key)
                continue

            bar_idx = max(0, i - 1)
            ts_ms = _candle_ts_ms(candles[bar_idx])
            if signal.direction == "BUY":
                trail = _TrailState(
                    peak=signal.entry,
                    trough=signal.entry,
                    effective_sl=signal.stop_loss,
                )
            else:
                trail = _TrailState(
                    peak=signal.entry,
                    trough=signal.entry,
                    effective_sl=signal.stop_loss,
                )
            open_trade = _ActiveTradeV3(
                signal=signal,
                signal_bar_idx=bar_idx,
                placed_bar_idx=i,
                order_id=f"breakout_v3_{pair_key}_{ts_ms}",
                trail=trail,
            )

        if open_trade is not None:
            if open_trade.filled:
                completed.append((open_trade.signal, "OPEN", 0.0))
            else:
                stats["expired_no_fill"] += 1
                await strategy.release_signal_block(pair_key)

    closed_trades = [(s, res, r) for s, res, r in completed if res in ("WIN", "LOSS")]
    closed_r = [r for _, _, r in closed_trades]

    wins = sum(1 for _, res, _ in closed_trades if res == "WIN")
    losses = sum(1 for _, res, _ in closed_trades if res == "LOSS")
    no_result = sum(1 for _, res, _ in completed if res == "OPEN")
    total_r = sum(closed_r)

    longs = sum(1 for s, _, _ in closed_trades if s.direction == "BUY")
    shorts = sum(1 for s, _, _ in closed_trades if s.direction == "SELL")
    closed = wins + losses

    profit_factor = _profit_factor(closed_r) if closed else None
    max_drawdown_r = _max_drawdown_r(closed_r) if closed else 0.0
    winrate = wins / closed * 100 if closed > 0 else 0.0
    avg_r = total_r / closed if closed > 0 else 0.0

    result_data = {
        "timeframe": tf,
        "bars": len(candles),
        "warmup_bars": warmup_bars,
        "total": stats["trades_filled"],
        "signals_generated": stats["signals_generated"],
        "rejected_spread": stats["rejected_spread"],
        "expired_no_fill": stats["expired_no_fill"],
        "longs": longs,
        "shorts": shorts,
        "wins": wins,
        "losses": losses,
        "no_result": no_result,
        "winrate": winrate,
        "breakeven": 50.0,
        "avg_rr": 0.0,
        "total_r": total_r,
        "avg_r": avg_r,
        "profitable": total_r > 0 if closed > 0 else False,
        "assumed_spread_pips": assumed_spread,
        "profit_factor": profit_factor,
        "max_drawdown_r": max_drawdown_r,
        "trail_pips": trail_pips,
    }

    if not silent:
        _print_results_v3(result_data, pair=pair)

    return result_data


def _print_results_v3(r: dict, pair: Optional[str] = None) -> None:
    profitable = "✅ ПРИБЫЛЬНО" if r["profitable"] else "❌ УБЫТОЧНО"
    pair_label = f"{pair} " if pair else ""
    logging.info("=" * 70)
    logging.info(
        f"📊 РЕЗУЛЬТАТЫ БЭКТЕСТА v3 ({pair_label}{r['bars']} свечей {r['timeframe']})"
    )
    logging.info("=" * 70)
    logging.info("  Режим                   : stop fill, trailing SL, no TP")
    logging.info(f"  Trail distance          : {r.get('trail_pips', 0):.1f} pips")
    logging.info(f"  Прогрев                 : {r.get('warmup_bars', 0)} свечей")
    logging.info(f"  Spread (assumed)        : {r.get('assumed_spread_pips', 0):.1f} pips")
    logging.info(f"  Сигналов сгенерировано  : {r.get('signals_generated', r['total'])}")
    logging.info(f"  Истекло (нет fill)      : {r.get('expired_no_fill', 0)}")
    logging.info(f"  Сделок (filled)         : {r['total']}")
    logging.info(f"  LONG / SHORT            : {r['longs']} / {r['shorts']}")
    logging.info(f"  Побед  (WIN)            : {r['wins']}")
    logging.info(f"  Потерь (LOSS)           : {r['losses']}")
    if r["wins"] + r["losses"] > 0:
        logging.info(f"  Winrate                 : {r['winrate']:.1f}%")
        logging.info(f"  {profitable}")
        logging.info(f"  Итого R                 : {r['total_r']:+.1f}R")
        logging.info(f"  Profit factor           : {_format_profit_factor(r.get('profit_factor'))}")
    logging.info("=" * 70)
