"""Breakout Retest v3 scanner implementing SetupScanner with immutable FSM."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.config.settings import config
from app.models.candle import Candle
from app.scanner.scanners.breakout.fsm import BreakoutFSMState, apply_transition
from app.scanner.types.enums import BreakoutState, Direction, SetupType
from app.scanner.types.market_context import MarketContext
from app.scanner.types.setup_candidate import SetupCandidate
from app.scanner.utils.candles import df_to_candles, signal_bar_index, validate_candles_df
from app.strategy.breakout_retest_v3 import (
    BreakoutSetup,
    build_signal,
    check_breakout_at,
    check_displacement,
    check_shallow_retest,
    detect_levels,
    displacement_size,
)

logger = logging.getLogger(__name__)

DEFAULT_TP_RR = 2.0


@dataclass
class BreakoutScanner:
    """
    Breakout Retest v3 as a SetupScanner.

    Wraps existing v3 quantitative logic without modifying the legacy strategy.
    FSM transitions are immutable; at most one state change per bar update.
    """

    pip_value: float = 0.0001
    pair_config: object | None = None
    pair_configs: dict[str, object] = field(default_factory=dict)
    pip_values: dict[str, float] = field(default_factory=dict)
    tp_rr: float = DEFAULT_TP_RR

    _states: dict[str, BreakoutFSMState] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    def scan(
        self,
        candles: pd.DataFrame,
        context: MarketContext,
    ) -> list[SetupCandidate]:
        validate_candles_df(candles)
        symbol = context.symbol
        pair_cfg = self._pair_cfg_for(symbol)
        if len(candles) < 5 or pair_cfg is None:
            return []

        pip = self._pip_for(symbol)
        bar_list = df_to_candles(candles)
        signal_idx = signal_bar_index(candles)
        signal_ts = bar_list[signal_idx].timestamp

        saved_cfg, saved_pip = self.pair_config, self.pip_value
        with self._lock:
            self.pair_config = pair_cfg
            self.pip_value = pip
            try:
                fsm = self._states.get(symbol, BreakoutFSMState())
                if fsm.last_processed_timestamp == signal_ts:
                    return []
                fsm = self._advance_one_bar(fsm, symbol, bar_list, signal_idx, context)
                self._states[symbol] = fsm
            finally:
                self.pair_config, self.pip_value = saved_cfg, saved_pip

        if fsm.state == BreakoutState.CONFIRMED and fsm.setup is not None:
            with self._lock:
                self.pair_config = pair_cfg
                self.pip_value = pip
                try:
                    candidate = self._build_candidate(
                        symbol, context.timeframe, fsm.setup, bar_list, signal_idx,
                    )
                    self._states[symbol] = apply_transition(
                        fsm,
                        BreakoutState.IDLE,
                        clear_setup=True,
                        last_processed_timestamp=signal_ts,
                    )
                finally:
                    self.pair_config, self.pip_value = saved_cfg, saved_pip
            return [candidate]

        return []

    def snapshot(self, candles: pd.DataFrame, context: MarketContext) -> SetupCandidate | None:
        """In-progress FSM state as a low-score watchlist candidate."""
        symbol = context.symbol
        pair_cfg = self._pair_cfg_for(symbol)
        if pair_cfg is None or candles.empty:
            return None

        pip = self._pip_for(symbol)
        with self._lock:
            fsm = self._states.get(symbol, BreakoutFSMState())

        if fsm.state in (BreakoutState.IDLE, BreakoutState.CONFIRMED, BreakoutState.INVALIDATED):
            return None
        if fsm.setup is None:
            return None

        setup = fsm.setup
        close_px = float(candles.iloc[-1]["close"])
        direction = Direction.BUY if setup.direction == "BUY" else Direction.SELL
        level = setup.level.price

        state_confidence = {
            BreakoutState.BREAKOUT_DETECTED: (0.45, "Awaiting displacement after breakout"),
            BreakoutState.AWAITING_RETEST: (0.65, "Awaiting shallow retest"),
        }
        confidence, phase_reason = state_confidence.get(fsm.state, (0.3, "Monitoring"))

        if direction == Direction.BUY:
            sl = level - pip * 10
            tp = close_px + abs(close_px - sl) * self.tp_rr
        else:
            sl = level + pip * 10
            tp = close_px - abs(close_px - sl) * self.tp_rr

        disp_pips = displacement_size(setup) / pip if pip > 0 else 0.0
        return SetupCandidate(
            symbol=symbol,
            timeframe=context.timeframe,
            setup_type=SetupType.BREAKOUT,
            direction=direction,
            score=confidence * 10.0,
            confidence=confidence,
            entry_price=close_px,
            stop_loss=sl,
            take_profit=tp,
            reasons=[
                phase_reason,
                f"Breakout {setup.direction} level {level:.5f}",
                f"Displacement {disp_pips:.1f} pips",
            ],
            timestamp=_ts_to_datetime(candles.iloc[-1]["timestamp"]),
            ai_explanation=None,
        )

    def set_pip_value(self, pair: str, pip_value: float) -> None:
        self.pip_values[pair] = pip_value

    def _pair_cfg_for(self, symbol: str) -> object | None:
        if symbol in self.pair_configs:
            return self.pair_configs[symbol]
        return self.pair_config

    def _pip_for(self, symbol: str) -> float:
        return self.pip_values.get(symbol, self.pip_value)

    def reset(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol is None:
                self._states.clear()
            else:
                self._states.pop(symbol, None)

    def get_fsm_state(self, symbol: str) -> BreakoutFSMState:
        with self._lock:
            return self._states.get(symbol, BreakoutFSMState())

    def _cfg_params(self) -> dict[str, Any]:
        c = self.pair_config
        if c is None:
            raise RuntimeError("BreakoutScanner: pair_config not set")
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
            "min_retrace_percent": getattr(c, "min_retrace_percent", 30.0),
            "max_retrace_percent": getattr(c, "max_retrace_percent", 70.0),
            "retest_timeout_bars": c.retest_timeout_bars,
            "retest_tolerance": c.retest_tolerance_pips,
            "sl_buffer": c.sl_buffer_pips,
            "timeframe": c.entry_timeframe,
        }

    def _advance_one_bar(
        self,
        fsm: BreakoutFSMState,
        symbol: str,
        bars: list[Candle],
        signal_idx: int,
        context: MarketContext,
    ) -> BreakoutFSMState:
        """Process exactly one closed bar with at most one FSM transition."""
        signal_ts = bars[signal_idx].timestamp
        handlers = {
            BreakoutState.IDLE: self._handle_idle,
            BreakoutState.BREAKOUT_DETECTED: self._handle_breakout_detected,
            BreakoutState.AWAITING_RETEST: self._handle_awaiting_retest,
            BreakoutState.CONFIRMED: self._handle_confirmed,
            BreakoutState.INVALIDATED: self._handle_invalidated,
        }
        handler = handlers.get(fsm.state)
        if handler is None:
            return apply_transition(fsm, BreakoutState.IDLE, last_processed_timestamp=signal_ts)

        new_fsm, _ = handler(fsm, symbol, bars, signal_idx, context)
        return replace_last_processed(new_fsm, signal_ts)

    def _handle_idle(
        self,
        fsm: BreakoutFSMState,
        symbol: str,
        bars: list[Candle],
        signal_idx: int,
        context: MarketContext,
    ) -> tuple[BreakoutFSMState, bool]:
        p = self._cfg_params()
        signal_dt = _ts_to_datetime(bars[signal_idx].timestamp)

        if not (config.TRADE_WINDOW_START <= signal_dt.time() <= config.TRADE_WINDOW_END):
            return fsm, False

        # ВСТАВИТЬ СУЩЕСТВУЮЩУЮ ЛОГИКУ V3 ЗДЕСЬ — level detection uses closed bars only
        if len(bars) < 5:
            return fsm, False

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
            return fsm, False

        # ВСТАВИТЬ СУЩЕСТВУЮЩУЮ ЛОГИКУ V3 ЗДЕСЬ — breakout detection
        setup, _reject = check_breakout_at(
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
            return fsm, False

        # logger.info(
        #     f"📈 [{symbol}] Breakout {setup.direction} level={setup.level.price:.5f}"
        # )
        return (
            apply_transition(fsm, BreakoutState.BREAKOUT_DETECTED, setup=setup),
            True,
        )

    def _handle_breakout_detected(
        self,
        fsm: BreakoutFSMState,
        symbol: str,
        bars: list[Candle],
        signal_idx: int,
        context: MarketContext,
    ) -> tuple[BreakoutFSMState, bool]:
        if fsm.setup is None:
            return apply_transition(fsm, BreakoutState.IDLE, clear_setup=True), True

        p = self._cfg_params()
        setup = fsm.setup

        # ВСТАВИТЬ СУЩЕСТВУЮЩУЮ ЛОГИКУ V3 ЗДЕСЬ — displacement check
        mutable_setup = _clone_setup(setup)
        disp, cancel_reason = check_displacement(
            bars,
            mutable_setup,
            signal_idx,
            min_displacement_pips=p["min_displacement"],
            max_displacement_pips=p["max_displacement"],
            max_bars_for_displacement=p["max_bars_for_displacement"],
            pip_value=self.pip_value,
        )
        setup = _clone_setup(mutable_setup)

        if disp == "CANCELLED":
            # logger.info(f"❌ [{symbol}] setup cancelled: {cancel_reason}")
            return apply_transition(fsm, BreakoutState.INVALIDATED, clear_setup=True), True

        if disp == "OK":
            updated_setup = _copy_setup(setup, setup_start_bar=signal_idx)
            disp_pips = displacement_size(updated_setup) / self.pip_value
            # logger.info(
            #     f"✅ [{symbol}] Displacement OK ({disp_pips:.1f}p) → await retest"
            # )
            return (
                apply_transition(
                    fsm,
                    BreakoutState.AWAITING_RETEST,
                    setup=updated_setup,
                ),
                True,
            )

        return fsm, False

    def _handle_awaiting_retest(
        self,
        fsm: BreakoutFSMState,
        symbol: str,
        bars: list[Candle],
        signal_idx: int,
        context: MarketContext,
    ) -> tuple[BreakoutFSMState, bool]:
        if fsm.setup is None:
            return apply_transition(fsm, BreakoutState.IDLE, clear_setup=True), True

        p = self._cfg_params()
        setup = fsm.setup
        bars_wait = signal_idx - setup.setup_start_bar

        if bars_wait > p["retest_timeout_bars"]:
            logger.debug(f"❌ [{symbol}] retest timeout")
            return apply_transition(fsm, BreakoutState.INVALIDATED, clear_setup=True), True

        # ВСТАВИТЬ СУЩЕСТВУЮЩУЮ ЛОГИКУ V3 ЗДЕСЬ — shallow retest validation
        mutable_setup = _clone_setup(setup)
        state, retrace_pct = check_shallow_retest(
            bars,
            signal_idx,
            mutable_setup,
            min_retrace_percent=p["min_retrace_percent"],
            max_retrace_percent=p["max_retrace_percent"],
            retest_tolerance_pips=p["retest_tolerance"],
            pip_value=self.pip_value,
        )
        setup = _clone_setup(mutable_setup)

        if state == "CANCELLED":
            # logger.info(
            #     f"❌ [{symbol}] shallow retest too deep "
            #     f"({retrace_pct:.0f}% > {p['max_retrace_percent']:.0f}%)"
            #     if retrace_pct is not None
            #     else f"❌ [{symbol}] shallow retest cancelled"
            # )
            return apply_transition(fsm, BreakoutState.INVALIDATED, clear_setup=True), True

        if state != "OK":
            return fsm, False

        if retrace_pct is not None:
            logger.info(
                f"✅ [{symbol}] Shallow retest confirmed retrace={retrace_pct:.0f}%"
            )
        else:
            logger.info(f"✅ [{symbol}] Shallow retest confirmed")
        return apply_transition(fsm, BreakoutState.CONFIRMED, setup=setup), True

    def _handle_confirmed(
        self,
        fsm: BreakoutFSMState,
        symbol: str,
        bars: list[Candle],
        signal_idx: int,
        context: MarketContext,
    ) -> tuple[BreakoutFSMState, bool]:
        return fsm, False

    def _handle_invalidated(
        self,
        fsm: BreakoutFSMState,
        symbol: str,
        bars: list[Candle],
        signal_idx: int,
        context: MarketContext,
    ) -> tuple[BreakoutFSMState, bool]:
        return apply_transition(fsm, BreakoutState.IDLE, clear_setup=True), True

    def _build_candidate(
        self,
        symbol: str,
        timeframe: str,
        setup: BreakoutSetup,
        bars: list[Candle],
        retest_bar_index: int,
    ) -> SetupCandidate:
        p = self._cfg_params()
        signal = build_signal(
            setup,
            bars,
            symbol,
            sl_buffer_pips=p["sl_buffer"],
            timeframe=timeframe,
            pip_value=self.pip_value,
            retest_bar_index=retest_bar_index,
        )

        direction = (
            Direction.BUY if setup.direction == "BUY" else Direction.SELL
        )
        entry = signal.entry
        sl = signal.stop_loss
        risk = abs(entry - sl)
        if direction == Direction.BUY:
            tp = entry + risk * self.tp_rr
        else:
            tp = entry - risk * self.tp_rr

        disp_pips = displacement_size(setup) / self.pip_value
        reasons = [
            f"Breakout {setup.direction} at level {setup.level.price:.5f}",
            f"Displacement {disp_pips:.1f} pips confirmed",
            "Shallow retest 30–70% zone validated",
        ]

        confidence = min(1.0, 0.5 + setup.level.touches * 0.1)

        return SetupCandidate(
            symbol=symbol,
            timeframe=timeframe,
            setup_type=SetupType.BREAKOUT,
            direction=direction,
            score=confidence * 10.0,
            confidence=confidence,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            reasons=reasons,
            timestamp=_ts_to_datetime(bars[retest_bar_index].timestamp),
            ai_explanation=None,
        )


def _ts_to_datetime(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc)


def _clone_setup(setup: BreakoutSetup) -> BreakoutSetup:
    return BreakoutSetup(
        level=setup.level,
        direction=setup.direction,
        breakout_bar_index=setup.breakout_bar_index,
        setup_start_bar=setup.setup_start_bar,
        max_high_since=setup.max_high_since,
        min_low_since=setup.min_low_since,
    )


def _copy_setup(setup: BreakoutSetup, **kwargs) -> BreakoutSetup:
    return BreakoutSetup(
        level=setup.level,
        direction=setup.direction,
        breakout_bar_index=setup.breakout_bar_index,
        setup_start_bar=kwargs.get("setup_start_bar", setup.setup_start_bar),
        max_high_since=setup.max_high_since,
        min_low_since=setup.min_low_since,
    )


def replace_last_processed(fsm: BreakoutFSMState, ts: object) -> BreakoutFSMState:
    from dataclasses import replace

    return replace(fsm, last_processed_timestamp=ts)
