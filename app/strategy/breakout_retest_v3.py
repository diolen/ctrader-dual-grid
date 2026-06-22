# app/strategy/breakout_retest_v3.py
"""
Breakout Retest Scalping v3 — shallow retracement + stop order entry.

После displacement: откат 30–70% от импульса → Buy/Sell Stop на high/low
ретестовой свечи. SL за структурой, trailing SL на брокере, без TP.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable, Any, Dict, List, Literal, Optional, Tuple

from app.config.settings import config
from app.models.candle import Candle
from app.models.signal import Signal as UnifiedSignal, SignalStatus
from app.strategy.base import BaseStrategy, MarketData
from app.strategy.fsm import StrategyState, StateMachine
from app.strategy.trade_guard import TradeGuard
from app.core.recommender import AnalysisResult, MarketPhase

logger = logging.getLogger(__name__)

LevelDirection = Literal["RESISTANCE", "SUPPORT"]
TradeDirection = Literal["BUY", "SELL"]
DisplacementState = Literal["WAIT", "OK", "CANCELLED"]
ShallowRetestState = Literal["WAIT", "OK", "CANCELLED"]
BreakoutRejectReason = Literal["low_volume"]

DEFAULT_MIN_RETRACE_PERCENT = 30.0
DEFAULT_MAX_RETRACE_PERCENT = 70.0


def _ts_to_datetime(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)


@dataclass
class Level:
    price: float
    touches: int
    direction: LevelDirection
    last_touch_bar: int
    first_touch_bar: int


@dataclass
class BreakoutSetup:
    level: Level
    direction: TradeDirection
    breakout_bar_index: int
    setup_start_bar: int = 0
    max_high_since: float = 0.0
    min_low_since: float = 0.0


COLD_START_LIVE_EMIT_MAX_BARS = 2


@dataclass
class PairStrategyState:
    fsm: StateMachine
    setup: Optional[BreakoutSetup] = None
    pair: str = ""
    pending_live_signal: bool = False


def _pips_to_price(pips: float, pip_value: float) -> float:
    return pips * pip_value


def _count_touches(
    bars: List[Candle],
    price: float,
    tol: float,
    start: int,
    end: int,
) -> tuple[int, int, int]:
    """(touches, first_bar, last_bar) в [start, end)."""
    touches = 0
    first_bar = -1
    last_bar = -1
    for i in range(start, end):
        b = bars[i]
        if abs(b.high - price) <= tol or abs(b.low - price) <= tol:
            touches += 1
            if first_bar < 0:
                first_bar = i
            last_bar = i
    return touches, first_bar, last_bar


def detect_levels(
    bars: List[Candle],
    *,
    lookback_bars: int,
    min_touches: int,
    touch_tolerance: float,
    cluster_pips: float,
    min_level_age_bars: int,
    max_level_age_bars: int,
    pip_value: float,
    current_bar: int,
) -> List[Level]:
    """Swing high/low + кластеризация → уровни S/R."""
    n = len(bars)
    if n < 5:
        return []

    start = max(1, n - lookback_bars)
    end = n - 1
    cluster_dist = _pips_to_price(cluster_pips, pip_value)
    tol = _pips_to_price(touch_tolerance, pip_value)

    raw: List[tuple[float, int, LevelDirection]] = []
    for i in range(start + 1, end):
        b = bars[i]
        if b.high > bars[i - 1].high and b.high > bars[i + 1].high:
            raw.append((b.high, i, "RESISTANCE"))
        if b.low < bars[i - 1].low and b.low < bars[i + 1].low:
            raw.append((b.low, i, "SUPPORT"))

    if not raw:
        return []

    raw.sort(key=lambda x: x[0])
    clusters: List[list] = []
    for price, bar_i, direction in raw:
        if not clusters or abs(price - clusters[-1][0][0]) > cluster_dist:
            clusters.append([(price, bar_i, direction)])
        else:
            clusters[-1].append((price, bar_i, direction))

    levels: List[Level] = []
    for cluster in clusters:
        avg_price = sum(c[0] for c in cluster) / len(cluster)
        direction = cluster[0][2]
        swing_touches = len(cluster)
        bar_touches, first_bar, last_bar = _count_touches(
            bars, avg_price, tol, start, end,
        )
        total_touches = max(swing_touches, bar_touches)
        if total_touches < min_touches:
            continue
        if first_bar < 0:
            first_bar = min(c[1] for c in cluster)
            last_bar = max(c[1] for c in cluster)
        age = current_bar - first_bar
        if age < min_level_age_bars or age > max_level_age_bars:
            continue
        levels.append(
            Level(
                price=avg_price,
                touches=total_touches,
                direction=direction,
                last_touch_bar=last_bar,
                first_touch_bar=first_bar,
            )
        )
    return levels


def bars_since_last_touch(
    bars: List[Candle],
    level: Level,
    signal_idx: int,
    touch_tolerance: float,
    pip_value: float,
) -> int:
    tol = _pips_to_price(touch_tolerance, pip_value)
    for i in range(signal_idx, -1, -1):
        b = bars[i]
        if abs(b.high - level.price) <= tol or abs(b.low - level.price) <= tol:
            return signal_idx - i
    return signal_idx - level.last_touch_bar


def _avg_volume(
    bars: List[Candle],
    end_idx: int,
    lookback_bars: int,
) -> float:
    start = max(0, end_idx - lookback_bars)
    segment = bars[start:end_idx]
    if not segment:
        return 0.0
    return sum(b.volume for b in segment) / len(segment)


def check_breakout_at(
    bars: List[Candle],
    signal_idx: int,
    levels: List[Level],
    *,
    close_buffer: float,
    min_body: float,
    max_bars_since_touch: int,
    touch_tolerance: float,
    pip_value: float,
    min_volume_mult: float = 0.0,
    volume_lookback_bars: int = 20,
) -> Tuple[Optional[BreakoutSetup], Optional[BreakoutRejectReason]]:
    bar = bars[signal_idx]
    buf = _pips_to_price(close_buffer, pip_value)
    min_body_px = _pips_to_price(min_body, pip_value)
    use_volume = min_volume_mult > 0.0
    avg_vol = (
        _avg_volume(bars, signal_idx, volume_lookback_bars) if use_volume else 0.0
    )
    last_reject: Optional[BreakoutRejectReason] = None
    candidates: List[Tuple[Level, int, BreakoutSetup]] = []

    for level in levels:
        since = bars_since_last_touch(
            bars, level, signal_idx, touch_tolerance, pip_value,
        )
        if since > max_bars_since_touch:
            continue

        if level.direction == "RESISTANCE":
            body = bar.close - bar.open
            if bar.close > level.price + buf and body >= min_body_px:
                if (
                    use_volume
                    and avg_vol > 0
                    and bar.volume < avg_vol * min_volume_mult
                ):
                    last_reject = "low_volume"
                    continue
                candidates.append((
                    level,
                    since,
                    BreakoutSetup(
                        level=level,
                        direction="BUY",
                        breakout_bar_index=signal_idx,
                        max_high_since=bar.high,
                        min_low_since=bar.low,
                    ),
                ))
        else:
            body = bar.open - bar.close
            if bar.close < level.price - buf and body >= min_body_px:
                if (
                    use_volume
                    and avg_vol > 0
                    and bar.volume < avg_vol * min_volume_mult
                ):
                    last_reject = "low_volume"
                    continue
                candidates.append((
                    level,
                    since,
                    BreakoutSetup(
                        level=level,
                        direction="SELL",
                        breakout_bar_index=signal_idx,
                        max_high_since=bar.high,
                        min_low_since=bar.low,
                    ),
                ))

    if not candidates:
        return None, last_reject

    candidates.sort(key=lambda item: (-item[0].touches, item[1]))
    return candidates[0][2], None


def check_displacement(
    bars: List[Candle],
    setup: BreakoutSetup,
    signal_idx: int,
    *,
    min_displacement_pips: float,
    max_displacement_pips: float,
    max_bars_for_displacement: int,
    pip_value: float,
) -> Tuple[DisplacementState, Optional[str]]:
    """
    Импульс после пробоя: min/max displacement + лимит баров на достижение min.
    """
    bar = bars[signal_idx]
    setup.max_high_since = max(setup.max_high_since, bar.high)
    setup.min_low_since = min(setup.min_low_since, bar.low)
    level = setup.level.price
    min_disp = _pips_to_price(min_displacement_pips, pip_value)
    max_disp = _pips_to_price(max_displacement_pips, pip_value)
    bars_elapsed = signal_idx - setup.breakout_bar_index

    if setup.direction == "BUY":
        move = setup.max_high_since - level
        if move > max_disp:
            return "CANCELLED", "max_displacement"
        if move >= min_disp:
            return "OK", None
    else:
        move = level - setup.min_low_since
        if move > max_disp:
            return "CANCELLED", "max_displacement"
        if move >= min_disp:
            return "OK", None

    if bars_elapsed >= max_bars_for_displacement:
        return "CANCELLED", "slow_displacement"
    return "WAIT", None


def displacement_size(setup: BreakoutSetup) -> float:
    """Максимальное удаление цены от уровня после пробоя."""
    level = setup.level.price
    if setup.direction == "BUY":
        return setup.max_high_since - level
    return level - setup.min_low_since


def _retest_near_level(
    bar: Candle,
    setup: BreakoutSetup,
    retest_tolerance_pips: float,
    pip_value: float,
) -> bool:
    """Цена вернулась к пробитому уровню (wick в зоне tolerance)."""
    if retest_tolerance_pips <= 0:
        return True
    tol = _pips_to_price(retest_tolerance_pips, pip_value)
    level = setup.level.price
    if setup.direction == "BUY":
        return bar.low <= level + tol
    return bar.high >= level - tol


def check_shallow_retest(
    bars: List[Candle],
    signal_idx: int,
    setup: BreakoutSetup,
    *,
    min_retrace_percent: float,
    max_retrace_percent: float,
    retest_tolerance_pips: float = 0.0,
    pip_value: float = 0.0001,
) -> Tuple[ShallowRetestState, Optional[float]]:
    """
    Shallow retest: откат 30–70% импульса + wick у пробитого уровня.
    """
    bar = bars[signal_idx]
    setup.max_high_since = max(setup.max_high_since, bar.high)
    setup.min_low_since = min(setup.min_low_since, bar.low)

    level = setup.level.price
    if setup.direction == "BUY":
        peak = setup.max_high_since
        disp = peak - level
        if disp <= 0:
            return "WAIT", None
        retrace = peak - bar.low
        retrace_pct = (retrace / disp) * 100.0
    else:
        trough = setup.min_low_since
        disp = level - trough
        if disp <= 0:
            return "WAIT", None
        retrace = bar.high - trough
        retrace_pct = (retrace / disp) * 100.0

    if retrace_pct > max_retrace_percent:
        return "CANCELLED", retrace_pct
    if retrace_pct >= min_retrace_percent:
        if _retest_near_level(bar, setup, retest_tolerance_pips, pip_value):
            return "OK", retrace_pct
        return "WAIT", retrace_pct
    return "WAIT", retrace_pct


def _local_structure_sl(
    bars: List[Candle],
    setup: BreakoutSetup,
    signal_idx: int,
    sl_buffer_pips: float,
    pip_value: float,
) -> float:
    """SL за локальный экстремум от бара пробоя до текущего закрытого бара."""
    buf = _pips_to_price(sl_buffer_pips, pip_value)
    start = setup.breakout_bar_index
    end = signal_idx + 1
    segment = bars[start:end]
    if not segment:
        level = setup.level.price
        return level - buf if setup.direction == "BUY" else level + buf

    if setup.direction == "BUY":
        local_min = min(b.low for b in segment)
        return local_min - buf
    local_max = max(b.high for b in segment)
    return local_max + buf


def build_signal(
    setup: BreakoutSetup,
    bars: List[Candle],
    pair: str,
    *,
    sl_buffer_pips: float,
    timeframe: str,
    pip_value: float,
    retest_bar_index: int,
) -> UnifiedSignal:
    """Stop order: BUY → high ретестовой свечи, SELL → low. Без TP."""
    level = setup.level.price
    bar = bars[retest_bar_index]
    dt = _ts_to_datetime(bar.timestamp)
    sl = _local_structure_sl(
        bars, setup, retest_bar_index, sl_buffer_pips, pip_value,
    )

    if setup.direction == "BUY":
        entry = bar.high
        direction_str = "BUY"
    else:
        entry = bar.low
        direction_str = "SELL"

    return UnifiedSignal(
        pair=pair,
        direction=direction_str,
        entry=entry,
        stop_loss=sl,
        take_profit=0.0,
        timestamp=dt,
        breakout_price=level,
        strategy_type="BREAKOUT_RETEST_V3",
        timeframe=timeframe,
        bias_timeframe=None,
        status=SignalStatus.PENDING,
    )


def _spread_price_from_market(
    market_data: MarketData,
    pip_value: float,
) -> Optional[float]:
    if market_data.spread is not None:
        return market_data.spread
    spread_pips = getattr(market_data, "spread_pips", None)
    if spread_pips is not None:
        return _pips_to_price(float(spread_pips), pip_value)
    return None


@dataclass
class BreakoutRetestScalpingV3Strategy(BaseStrategy):
    """
    Breakout Retest Scalping v3: shallow retest + stop order, trailing SL, no TP.
    """
    pip_value: float = field(default=0.0001)
    pair_config: object = field(default=None)
    pair_configs: Dict[str, object] = field(default_factory=dict)
    trade_guard: TradeGuard = field(default_factory=TradeGuard)

    _state_by_pair: Dict[str, PairStrategyState] = field(default_factory=dict, init=False)
    _signal_timestamp_by_pair: Dict[str, datetime] = field(default_factory=dict, init=False)
    _fsm_timeout_handler: Optional[Callable[[str], Awaitable[Any]]] = field(
        default=None, init=False,
    )
    _filter_stats: Dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._reset_filter_stats()

    def _reset_filter_stats(self) -> None:
        self._filter_stats = {
            "ticks_in_window": 0,
            "window_fail": 0,
            "spread_too_high": 0,
            "no_level": 0,
            "insufficient_touches": 0,
            "fake_breakout": 0,
            "low_volume": 0,
            "no_displacement": 0,
            "displacement_too_large": 0,
            "slow_displacement": 0,
            "retest_timeout": 0,
            "shallow_retest_cancelled": 0,
            "signals": 0,
            "guard_block": 0,
            "fsm_entry_timeout": 0,
        }

    def set_fsm_timeout_handler(
        self, handler: Optional[Callable[[str], Awaitable[Any]]],
    ) -> None:
        self._fsm_timeout_handler = handler

    def _get_state(self, pair: str) -> PairStrategyState:
        if pair not in self._state_by_pair:
            self._state_by_pair[pair] = PairStrategyState(
                fsm=StateMachine(StrategyState.SCAN_LEVELS),
                pair=pair,
            )
        return self._state_by_pair[pair]

    def _pair_cfg(self):
        if not self.pair_config:
            raise RuntimeError("BreakoutRetestScalpingV3Strategy: pair_config не задан")
        return self.pair_config

    def _pair_cfg_for(self, pair: str) -> Optional[object]:
        if self.pair_configs and pair in self.pair_configs:
            return self.pair_configs[pair]
        return self.pair_config

    def _retest_timeout_bars_for(self, pair: str) -> int:
        pc = self._pair_cfg_for(pair)
        return pc.retest_timeout_bars if pc else 20

    def _cfg_params(self) -> dict:
        c = self._pair_cfg()
        return {
            "lookback_bars": c.level_lookback_bars,
            "min_touches": c.min_touches,
            "touch_tolerance": c.touch_tolerance_pips,
            "cluster_pips": c.level_cluster_pips,
            "min_level_age_bars": c.min_level_age_bars,
            "max_level_age_bars": c.max_level_age_bars,
            "close_buffer": c.breakout_close_buffer_pips,
            "min_body": c.min_body_pips,
            "max_bars_since_touch": c.max_bars_since_touch,
            "min_displacement": c.min_displacement_pips,
            "max_displacement": c.max_displacement_pips,
            "max_bars_for_displacement": c.max_bars_for_displacement,
            "min_volume_mult": c.min_volume_mult,
            "volume_lookback_bars": c.volume_lookback_bars,
            "min_retrace_percent": getattr(
                c, "min_retrace_percent", DEFAULT_MIN_RETRACE_PERCENT,
            ),
            "max_retrace_percent": getattr(
                c, "max_retrace_percent", DEFAULT_MAX_RETRACE_PERCENT,
            ),
            "retest_timeout_bars": c.retest_timeout_bars,
            "retest_tolerance": c.retest_tolerance_pips,
            "sl_buffer": c.sl_buffer_pips,
            "max_spread_pips": c.max_spread_pips,
            "timeframe": c.entry_timeframe,
        }

    def _to_scan_levels(self, pair: str) -> None:
        st = self._get_state(pair)
        st.setup = None
        st.pending_live_signal = False
        st.fsm.reset(StrategyState.SCAN_LEVELS)
        self._signal_timestamp_by_pair.pop(pair, None)

    def _cancel_reason_log(
        self,
        pair: str,
        reason: str,
        *,
        timeout_bars: Optional[int] = None,
        retrace_pct: Optional[float] = None,
        max_retrace_percent: Optional[float] = None,
    ) -> str:
        if reason == "retest_timeout":
            n = timeout_bars if timeout_bars is not None else self._pair_cfg().retest_timeout_bars
            return f"❌ [{pair}] retest timeout — shallow retest не за {n} бар(ов)"
        if reason == "shallow_retest_too_deep":
            if retrace_pct is not None and max_retrace_percent is not None:
                return (
                    f"❌ [{pair}] shallow retest too deep "
                    f"({retrace_pct:.0f}% > {max_retrace_percent:.0f}%)"
                )
        if reason == "slow_displacement":
            return (
                f"⏱️ [{pair}] slow_displacement — импульс не за "
                f"{self._pair_cfg().max_bars_for_displacement} бар(ов)"
            )
        if reason == "max_displacement":
            return f"❌ [{pair}] displacement слишком большой"
        return f"❌ [{pair}] setup cancelled: {reason}"

    def _to_cancelled(
        self,
        pair: str,
        reason: str,
        *,
        timeout_bars: Optional[int] = None,
        retrace_pct: Optional[float] = None,
        max_retrace_percent: Optional[float] = None,
    ) -> None:
        st = self._get_state(pair)
        st.fsm.transition_to(StrategyState.CANCELLED)
        logger.info(
            self._cancel_reason_log(
                pair,
                reason,
                timeout_bars=timeout_bars,
                retrace_pct=retrace_pct,
                max_retrace_percent=max_retrace_percent,
            )
        )
        st.fsm.transition_to(StrategyState.SCAN_LEVELS)
        st.setup = None

    def _handle_displacement_cancel(
        self,
        pair: str,
        cancel_reason: Optional[str],
    ) -> None:
        if cancel_reason == "slow_displacement":
            self._filter_stats["slow_displacement"] += 1
            self._to_cancelled(pair, "slow_displacement")
        else:
            self._filter_stats["displacement_too_large"] += 1
            self._to_cancelled(pair, "max_displacement")

    async def release_signal_block(self, pair: str) -> None:
        self._to_scan_levels(pair)
        await self.trade_guard.clear_pending(pair)

    def on_order_filled(self, pair: str) -> None:
        st = self._get_state(pair)
        if st.fsm.current_state == StrategyState.ENTRY_SUBMITTED:
            st.fsm.transition_to(StrategyState.IN_TRADE)

    def needs_update_without_new_bar(self, pair: str) -> bool:
        st = self._state_by_pair.get(pair)
        if not st:
            return False
        if st.pending_live_signal:
            return True
        return st.fsm.current_state == StrategyState.ENTRY_SUBMITTED

    def pair_status_line(self, pair: str, *, bars_count: int = 0) -> str:
        st = self._state_by_pair.get(pair)
        if not st or not st.setup:
            state = st.fsm.current_state.name if st else "SCAN_LEVELS"
            return f"{state}"
        extra = " →live" if st.pending_live_signal else ""
        lvl = st.setup.level.price
        wait_info = ""
        if (
            st.fsm.current_state == StrategyState.WAIT_RETEST
            and bars_count >= 2
        ):
            timeout = self._retest_timeout_bars_for(pair)
            bars_wait = (bars_count - 2) - st.setup.setup_start_bar
            disp_pips = displacement_size(st.setup) / self.pip_value
            wait_info = f" wait={bars_wait}/{timeout} disp={disp_pips:.1f}p"
        return (
            f"{st.fsm.current_state.name} {st.setup.direction} "
            f"lvl={lvl:.5f}{wait_info}{extra}"
        )

    async def _emit_stop_signal(
        self,
        pair: str,
        bars: List[Candle],
        st: PairStrategyState,
        p: dict,
        signal_dt: datetime,
        retest_bar_index: int,
        *,
        source: str = "shallow_retest",
        retrace_pct: Optional[float] = None,
    ) -> Optional[UnifiedSignal]:
        if not st.setup:
            return None
        if not await self.trade_guard.can_trade(pair):
            self._filter_stats["guard_block"] += 1
            return None

        signal = build_signal(
            st.setup,
            bars,
            pair,
            sl_buffer_pips=p["sl_buffer"],
            timeframe=p["timeframe"],
            pip_value=self.pip_value,
            retest_bar_index=retest_bar_index,
        )
        self._filter_stats["signals"] += 1
        st.fsm.transition_to(StrategyState.ENTRY_SUBMITTED)
        self._signal_timestamp_by_pair[pair] = signal_dt
        retrace_info = f" retrace={retrace_pct:.0f}%" if retrace_pct is not None else ""
        logger.info(
            f"✅ [{pair}] STOP signal ({source}) {signal.direction} "
            f"stop={signal.entry:.5f} SL={signal.stop_loss:.5f} "
            f"no TP | trailing SL{retrace_info}"
        )
        return signal

    def cold_start(self, bars: List[Candle], pair: str) -> None:
        """Восстановление FSM из истории M5 без персистентности."""
        self._to_scan_levels(pair)
        n = len(bars)
        if n < 10:
            return

        p = self._cfg_params()
        timeout = p["retest_timeout_bars"]
        scan_from = max(2, n - timeout - 5)

        last_closed_idx = n - 2
        for signal_idx in range(last_closed_idx, scan_from - 1, -1):
            window = bars[: signal_idx + 2]
            if len(window) < 5:
                continue
            levels = detect_levels(
                window,
                lookback_bars=p["lookback_bars"],
                min_touches=p["min_touches"],
                touch_tolerance=p["touch_tolerance"],
                cluster_pips=p["cluster_pips"],
                min_level_age_bars=p["min_level_age_bars"],
                max_level_age_bars=p["max_level_age_bars"],
                pip_value=self.pip_value,
                current_bar=signal_idx,
            )
            setup, reject = check_breakout_at(
                window,
                signal_idx,
                levels,
                close_buffer=p["close_buffer"],
                min_body=p["min_body"],
                max_bars_since_touch=p["max_bars_since_touch"],
                touch_tolerance=p["touch_tolerance"],
                pip_value=self.pip_value,
                min_volume_mult=p["min_volume_mult"],
                volume_lookback_bars=p["volume_lookback_bars"],
            )
            if reject == "low_volume":
                continue
            if not setup:
                continue

            st = self._get_state(pair)
            st.setup = setup
            st.fsm.transition_to(StrategyState.DISPLACEMENT_WAIT)
            skip_setup = False

            for j in range(setup.breakout_bar_index + 1, n - 1):
                disp, cancel_reason = check_displacement(
                    bars[: j + 2],
                    setup,
                    j,
                    min_displacement_pips=p["min_displacement"],
                    max_displacement_pips=p["max_displacement"],
                    max_bars_for_displacement=p["max_bars_for_displacement"],
                    pip_value=self.pip_value,
                )
                if disp == "CANCELLED":
                    if cancel_reason == "slow_displacement":
                        self._filter_stats["slow_displacement"] += 1
                    else:
                        self._filter_stats["displacement_too_large"] += 1
                    self._to_scan_levels(pair)
                    skip_setup = True
                    break
                if disp == "OK":
                    setup.setup_start_bar = j
                    st.fsm.transition_to(StrategyState.WAIT_RETEST)
                    break

            if skip_setup:
                continue

            if st.fsm.current_state != StrategyState.WAIT_RETEST:
                self._to_scan_levels(pair)
                continue

            for j in range(setup.setup_start_bar + 1, n - 1):
                if j - setup.setup_start_bar > timeout:
                    self._to_scan_levels(pair)
                    skip_setup = True
                    break
                state, retrace_pct = check_shallow_retest(
                    bars[: j + 2],
                    j,
                    setup,
                    min_retrace_percent=p["min_retrace_percent"],
                    max_retrace_percent=p["max_retrace_percent"],
                    retest_tolerance_pips=p["retest_tolerance"],
                    pip_value=self.pip_value,
                )
                if state == "CANCELLED":
                    self._filter_stats["shallow_retest_cancelled"] += 1
                    self._to_scan_levels(pair)
                    skip_setup = True
                    break
                if state == "OK":
                    st.fsm.transition_to(StrategyState.WAIT_RETEST)
                    bars_since_retest = last_closed_idx - j
                    if bars_since_retest <= COLD_START_LIVE_EMIT_MAX_BARS:
                        st.pending_live_signal = True
                        st.setup = setup
                        logger.info(
                            f"[{pair}] cold_start: {setup.direction} "
                            f"level={setup.level.price:.5f} — "
                            f"shallow retest {bars_since_retest} бар(ов) назад "
                            f"({retrace_pct:.0f}%), сигнал на первом live-tick"
                        )
                    else:
                        logger.info(
                            f"[{pair}] cold_start: {setup.direction} "
                            f"level={setup.level.price:.5f} — "
                            f"shallow retest {bars_since_retest} баров назад, ждём новый"
                        )
                    return

            if skip_setup:
                continue

            bars_left = (n - 2) - setup.setup_start_bar
            if bars_left <= timeout:
                logger.info(
                    f"[{pair}] cold_start: WAIT_RETEST {setup.direction} "
                    f"level={setup.level.price:.5f} bars_left={bars_left}"
                )
                return

            self._to_scan_levels(pair)
            return

        logger.debug(f"[{pair}] cold_start: SCAN_LEVELS")

    def _check_spread(self, market_data: MarketData, p: dict, pair: str) -> bool:
        spread_price = _spread_price_from_market(market_data, self.pip_value)
        if spread_price is None:
            return True
        max_spread_price = _pips_to_price(p["max_spread_pips"], self.pip_value)
        if spread_price > max_spread_price:
            self._filter_stats["spread_too_high"] += 1
            logger.debug(
                f"[{pair}] spread_too_high: {spread_price:.6f} > "
                f"{max_spread_price:.6f} ({p['max_spread_pips']}p)"
            )
            return False
        return True

    async def update(self, market_data: MarketData) -> Optional[UnifiedSignal]:
        pair = market_data.pair
        bars = market_data.candles

        if market_data.is_warmup:
            return None

        if len(bars) < 5:
            return None

        signal_idx = len(bars) - 2
        signal_dt = _ts_to_datetime(bars[signal_idx].timestamp)
        now_dt = datetime.now(timezone.utc)

        if not (config.TRADE_WINDOW_START <= signal_dt.time() <= config.TRADE_WINDOW_END):
            self._filter_stats["window_fail"] += 1
            return None

        p = self._cfg_params()
        if not self._check_spread(market_data, p, pair):
            return None

        self._filter_stats["ticks_in_window"] += 1
        st = self._get_state(pair)
        fsm = st.fsm

        if fsm.current_state == StrategyState.ENTRY_SUBMITTED:
            signal_ts = self._signal_timestamp_by_pair.get(pair)
            if signal_ts is not None:
                elapsed = (now_dt - signal_ts).total_seconds()
                if elapsed > float(config.PENDING_ORDER_MAX_AGE_SECONDS):
                    self._filter_stats["fsm_entry_timeout"] += 1
                    logger.warning(
                        f"⏰ [{pair}] Pending stop timeout ({elapsed:.0f}s)"
                    )
                    self._to_scan_levels(pair)
                    if self._fsm_timeout_handler:
                        await self._fsm_timeout_handler(pair)
                return None

        if fsm.current_state in (StrategyState.IN_TRADE, StrategyState.SIGNAL):
            return None

        if fsm.current_state == StrategyState.SCAN_LEVELS:
            if not await self.trade_guard.can_trade(pair):
                self._filter_stats["guard_block"] += 1
                return None

            levels = detect_levels(
                bars,
                lookback_bars=p["lookback_bars"],
                min_touches=p["min_touches"],
                touch_tolerance=p["touch_tolerance"],
                cluster_pips=p["cluster_pips"],
                min_level_age_bars=p["min_level_age_bars"],
                max_level_age_bars=p["max_level_age_bars"],
                pip_value=self.pip_value,
                current_bar=signal_idx,
            )
            if not levels:
                self._filter_stats["no_level"] += 1
                return None

            setup, reject = check_breakout_at(
                bars,
                signal_idx,
                levels,
                close_buffer=p["close_buffer"],
                min_body=p["min_body"],
                max_bars_since_touch=p["max_bars_since_touch"],
                touch_tolerance=p["touch_tolerance"],
                pip_value=self.pip_value,
                min_volume_mult=p["min_volume_mult"],
                volume_lookback_bars=p["volume_lookback_bars"],
            )
            if not setup:
                if reject == "low_volume":
                    self._filter_stats["low_volume"] += 1
                    logger.debug(
                        f"[{pair}] low_volume: пробой отклонён "
                        f"(mult>={p['min_volume_mult']})"
                    )
                return None

            st.setup = setup
            fsm.transition_to(StrategyState.DISPLACEMENT_WAIT)
            logger.info(
                f"📈 [{pair}] Breakout {setup.direction} level={setup.level.price:.5f}"
            )
            return None

        if fsm.current_state == StrategyState.DISPLACEMENT_WAIT:
            if not st.setup:
                self._to_scan_levels(pair)
                return None
            disp, cancel_reason = check_displacement(
                bars,
                st.setup,
                signal_idx,
                min_displacement_pips=p["min_displacement"],
                max_displacement_pips=p["max_displacement"],
                max_bars_for_displacement=p["max_bars_for_displacement"],
                pip_value=self.pip_value,
            )
            if disp == "CANCELLED":
                self._handle_displacement_cancel(pair, cancel_reason)
                return None
            if disp == "OK":
                st.setup.setup_start_bar = signal_idx
                fsm.transition_to(StrategyState.WAIT_RETEST)
                disp_pips = displacement_size(st.setup) / self.pip_value
                logger.info(
                    f"✅ [{pair}] Displacement OK ({disp_pips:.1f}p) → WAIT shallow retest"
                )
            else:
                self._filter_stats["no_displacement"] += 1
            return None

        if fsm.current_state == StrategyState.WAIT_RETEST:
            if not st.setup:
                self._to_scan_levels(pair)
                return None

            bars_wait = signal_idx - st.setup.setup_start_bar
            if bars_wait > p["retest_timeout_bars"]:
                self._filter_stats["retest_timeout"] += 1
                self._to_cancelled(
                    pair,
                    "retest_timeout",
                    timeout_bars=p["retest_timeout_bars"],
                )
                return None

            if st.pending_live_signal:
                st.pending_live_signal = False
                return await self._emit_stop_signal(
                    pair, bars, st, p, signal_dt, signal_idx,
                    source="cold_start live",
                )

            state, retrace_pct = check_shallow_retest(
                bars,
                signal_idx,
                st.setup,
                min_retrace_percent=p["min_retrace_percent"],
                max_retrace_percent=p["max_retrace_percent"],
                retest_tolerance_pips=p["retest_tolerance"],
                pip_value=self.pip_value,
            )
            if state == "CANCELLED":
                self._filter_stats["shallow_retest_cancelled"] += 1
                self._to_cancelled(
                    pair,
                    "shallow_retest_too_deep",
                    retrace_pct=retrace_pct,
                    max_retrace_percent=p["max_retrace_percent"],
                )
                return None
            if state != "OK":
                bar = bars[signal_idx]
                disp_pips = displacement_size(st.setup) / self.pip_value
                logger.info(
                    f"⏳ [{pair}] WAIT shallow retest {st.setup.direction} — "
                    f"retrace={retrace_pct:.0f}% "
                    f"(нужно {p['min_retrace_percent']:.0f}-{p['max_retrace_percent']:.0f}%) "
                    f"disp={disp_pips:.1f}p bar H/L={bar.high:.5f}/{bar.low:.5f} | "
                    f"бар {bars_wait}/{p['retest_timeout_bars']}"
                    if retrace_pct is not None
                    else f"⏳ [{pair}] WAIT shallow retest — нет отката | "
                    f"бар {bars_wait}/{p['retest_timeout_bars']}"
                )
                return None

            return await self._emit_stop_signal(
                pair, bars, st, p, signal_dt, signal_idx,
                source="shallow_retest",
                retrace_pct=retrace_pct,
            )

        return None

    def get_strategy_type(self) -> str:
        return "BREAKOUT_RETEST_V3"

    def get_timeframe(self) -> str:
        return self._pair_cfg().entry_timeframe

    async def reset(self, pair: Optional[str] = None, *, clear_guard: bool = True) -> None:
        if pair:
            self._to_scan_levels(pair)
            if clear_guard:
                try:
                    await self.trade_guard.reset(pair)
                except Exception as e:
                    logger.warning(f"TradeGuard reset failed для {pair}: {e}", exc_info=True)
        else:
            self._state_by_pair.clear()
            self._signal_timestamp_by_pair.clear()
            if clear_guard:
                try:
                    await self.trade_guard.reset()
                except Exception as e:
                    logger.warning(f"TradeGuard reset failed: {e}", exc_info=True)

    def filter_stats(self) -> dict:
        return {
            **self._filter_stats,
            "trade_window": (
                f"{config.TRADE_WINDOW_START.strftime('%H:%M')}-"
                f"{config.TRADE_WINDOW_END.strftime('%H:%M')} UTC"
            ),
        }

    def print_debug(self) -> None:
        fs = self.filter_stats()
        logger.info(
            f"Breakout v3 stats: window={fs['ticks_in_window']} signals={fs['signals']} "
            f"spread={fs['spread_too_high']} low_vol={fs['low_volume']} "
            f"slow_disp={fs['slow_displacement']} disp_cancel={fs['displacement_too_large']} "
            f"retest_timeout={fs['retest_timeout']} "
            f"shallow_cancel={fs['shallow_retest_cancelled']}"
        )

    def get_analysis_result(self, pair: str, candles: List[Candle]) -> Optional[AnalysisResult]:
        """
        Возвращает аналитический результат для скринер режима.
        Конвертирует текущее состояние FSM в AnalysisResult.
        """
        st = self._state_by_pair.get(pair)
        if not st or not candles:
            return None

        current_price = candles[-1].close if candles else 0.0
        fsm_state = st.fsm.current_state
        
        # Mapping FSM states to MarketPhase
        phase_map = {
            StrategyState.SCAN_LEVELS: MarketPhase.SCAN_LEVELS,
            StrategyState.DISPLACEMENT_WAIT: MarketPhase.DISPLACEMENT_WAIT,
            StrategyState.WAIT_RETEST: MarketPhase.WAIT_RETEST,
            StrategyState.ENTRY_SUBMITTED: MarketPhase.SETUP_READY,
            StrategyState.IN_TRADE: MarketPhase.SETUP_READY,
            StrategyState.CANCELLED: MarketPhase.SETUP_MISSED,
        }
        
        phase = phase_map.get(fsm_state, MarketPhase.SCAN_LEVELS)
        
        key_level = None
        direction = None
        displacement_pips = None
        retrace_percent = None
        wait_bars = None
        timeout_bars = None
        
        if st.setup:
            key_level = st.setup.level.price
            direction = st.setup.direction
            
            # Calculate displacement in pips
            disp_size = displacement_size(st.setup)
            displacement_pips = disp_size / self.pip_value if self.pip_value > 0 else 0.0
            
            # Calculate wait bars and timeout for WAIT_RETEST phase
            if phase == MarketPhase.WAIT_RETEST and len(candles) >= 2:
                wait_bars = (len(candles) - 2) - st.setup.setup_start_bar
                timeout_bars = self._retest_timeout_bars_for(pair)
                
                # Calculate retrace percent
                if st.setup.direction == "BUY":
                    peak = st.setup.max_high_since
                    disp = peak - key_level
                    if disp > 0:
                        retrace = peak - candles[-1].low
                        retrace_percent = (retrace / disp) * 100.0
                else:
                    trough = st.setup.min_low_since
                    disp = key_level - trough
                    if disp > 0:
                        retrace = candles[-1].high - trough
                        retrace_percent = (retrace / disp) * 100.0
        
        return AnalysisResult(
            pair=pair,
            phase=phase,
            current_price=current_price,
            key_level=key_level,
            direction=direction,
            displacement_pips=displacement_pips,
            retrace_percent=retrace_percent,
            wait_bars=wait_bars,
            timeout_bars=timeout_bars,
        )
