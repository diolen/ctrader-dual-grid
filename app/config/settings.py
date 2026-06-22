import os
from dotenv import load_dotenv
from dataclasses import dataclass, field
from datetime import time
from typing import List

load_dotenv()

# 7 мажорных пар Forex (G10)
MAJOR_FOREX_PAIRS: tuple[str, ...] = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
)

# Металлы и крипто
METAL_CRYPTO_PAIRS: tuple[str, ...] = (
    "XAUUSD",
    "BTCUSD",
)

DEFAULT_PAIRS: tuple[str, ...] = MAJOR_FOREX_PAIRS + METAL_CRYPTO_PAIRS

# Параметры v3 / screener ({PAIR}_BREAKOUT_* в .env)
_BREAKOUT_DEFAULTS: dict[str, object] = {
    "BREAKOUT_ENTRY_TIMEFRAME": "M5",
    "BREAKOUT_RISK_PCT": 0.5,
    "BREAKOUT_MAX_SPREAD_PIPS": 2.0,
    "BREAKOUT_LEVEL_LOOKBACK_BARS": 100,
    "BREAKOUT_MIN_TOUCHES": 2,
    "BREAKOUT_TOUCH_TOLERANCE_PIPS": 3.0,
    "BREAKOUT_LEVEL_CLUSTER_PIPS": 5.0,
    "BREAKOUT_MIN_LEVEL_AGE_BARS": 3,
    "BREAKOUT_MAX_LEVEL_AGE_BARS": 150,
    "BREAKOUT_CLOSE_BUFFER_PIPS": 1.0,
    "BREAKOUT_MIN_BODY_PIPS": 3.0,
    "BREAKOUT_MAX_BARS_SINCE_TOUCH": 20,
    "BREAKOUT_MIN_DISPLACEMENT_PIPS": 5.0,
    "BREAKOUT_MAX_DISPLACEMENT_PIPS": 30.0,
    "BREAKOUT_RETEST_TOLERANCE_PIPS": 4.0,
    "BREAKOUT_RETEST_TIMEOUT_BARS": 20,
    "BREAKOUT_SL_BUFFER_PIPS": 0.5,
    "BREAKOUT_MAX_BARS_FOR_DISPLACEMENT": 3,
    "BREAKOUT_MIN_VOLUME_MULT": 0.0,
    "BREAKOUT_VOLUME_LOOKBACK_BARS": 20,
    "BREAKOUT_MIN_RETRACE_PERCENT": 30.0,
    "BREAKOUT_MAX_RETRACE_PERCENT": 70.0,
    "BREAKOUT_V3_TRAIL_PIPS": 8.0,
}

# Только отличия от _BREAKOUT_DEFAULTS по парам
_BREAKOUT_OVERRIDES: dict[str, dict[str, object]] = {
    "EURUSD": {
        "BREAKOUT_V3_TRAIL_PIPS": 6.0,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 4.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 15,
        "BREAKOUT_MAX_RETRACE_PERCENT": 75.0,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 6.0,
    },
    "GBPUSD": {
        "BREAKOUT_V3_TRAIL_PIPS": 10.0,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 6.0,
        "BREAKOUT_MAX_BARS_FOR_DISPLACEMENT": 2,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 20,
        "BREAKOUT_MIN_RETRACE_PERCENT": 35.0,
        "BREAKOUT_MAX_RETRACE_PERCENT": 75.0,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 6.0,
        "BREAKOUT_SL_BUFFER_PIPS": 1.0,
    },
    "USDJPY": {
        "BREAKOUT_MAX_SPREAD_PIPS": 3.0,
        "BREAKOUT_CLOSE_BUFFER_PIPS": 2.0,
        "BREAKOUT_MIN_BODY_PIPS": 5.0,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 8.0,
        "BREAKOUT_MAX_DISPLACEMENT_PIPS": 50.0,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 8.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 18,
        "BREAKOUT_SL_BUFFER_PIPS": 1.0,
        "BREAKOUT_V3_TRAIL_PIPS": 12.0,
    },
    "USDCHF": {
        "BREAKOUT_MAX_SPREAD_PIPS": 2.5,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 4.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 15,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 5.0,
        "BREAKOUT_V3_TRAIL_PIPS": 6.0,
    },
    "AUDUSD": {
        "BREAKOUT_MAX_SPREAD_PIPS": 2.5,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 5.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 18,
        "BREAKOUT_MAX_RETRACE_PERCENT": 75.0,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 5.0,
        "BREAKOUT_V3_TRAIL_PIPS": 8.0,
    },
    "USDCAD": {
        "BREAKOUT_MAX_SPREAD_PIPS": 2.5,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 5.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 18,
        "BREAKOUT_MAX_RETRACE_PERCENT": 75.0,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 5.0,
        "BREAKOUT_V3_TRAIL_PIPS": 8.0,
    },
    "NZDUSD": {
        "BREAKOUT_MAX_SPREAD_PIPS": 3.0,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 5.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 20,
        "BREAKOUT_MIN_RETRACE_PERCENT": 35.0,
        "BREAKOUT_MAX_RETRACE_PERCENT": 75.0,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 6.0,
        "BREAKOUT_SL_BUFFER_PIPS": 1.0,
        "BREAKOUT_V3_TRAIL_PIPS": 9.0,
    },
    "XAUUSD": {
        "BREAKOUT_MAX_SPREAD_PIPS": 35.0,
        "BREAKOUT_TOUCH_TOLERANCE_PIPS": 12.0,
        "BREAKOUT_LEVEL_CLUSTER_PIPS": 15.0,
        "BREAKOUT_CLOSE_BUFFER_PIPS": 8.0,
        "BREAKOUT_MIN_BODY_PIPS": 15.0,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 30.0,
        "BREAKOUT_MAX_DISPLACEMENT_PIPS": 2000.0,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 25.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 15,
        "BREAKOUT_SL_BUFFER_PIPS": 15.0,
        "BREAKOUT_V3_TRAIL_PIPS": 40.0,
    },
    "BTCUSD": {
        "BREAKOUT_MAX_SPREAD_PIPS": 80.0,
        "BREAKOUT_TOUCH_TOLERANCE_PIPS": 30.0,
        "BREAKOUT_LEVEL_CLUSTER_PIPS": 40.0,
        "BREAKOUT_CLOSE_BUFFER_PIPS": 20.0,
        "BREAKOUT_MIN_BODY_PIPS": 40.0,
        "BREAKOUT_MIN_DISPLACEMENT_PIPS": 80.0,
        "BREAKOUT_MAX_DISPLACEMENT_PIPS": 800.0,
        "BREAKOUT_MAX_BARS_FOR_DISPLACEMENT": 2,
        "BREAKOUT_RETEST_TOLERANCE_PIPS": 60.0,
        "BREAKOUT_RETEST_TIMEOUT_BARS": 15,
        "BREAKOUT_SL_BUFFER_PIPS": 40.0,
        "BREAKOUT_V3_TRAIL_PIPS": 120.0,
    },
}

def _parse_time(value: str, default: time) -> time:
    try:
        h, m = value.strip().split(":")
        return time(int(h), int(m))
    except Exception:
        return default


def _parse_pairs(value: str) -> List[str]:
    seen: set[str] = set()
    pairs: List[str] = []
    for raw in value.split(","):
        pair = raw.strip().upper()
        if pair and pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return pairs


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class PairConfig:
    """Параметры Breakout Retest v3 / screener ({PAIR}_BREAKOUT_* в .env)."""
    pair: str
    entry_timeframe: str
    risk_pct: float
    max_spread_pips: float
    level_lookback_bars: int
    min_touches: int
    touch_tolerance_pips: float
    level_cluster_pips: float
    min_level_age_bars: int
    max_level_age_bars: int
    breakout_close_buffer_pips: float
    min_body_pips: float
    max_bars_since_touch: int
    min_displacement_pips: float
    max_displacement_pips: float
    retest_tolerance_pips: float
    retest_timeout_bars: int
    sl_buffer_pips: float
    max_bars_for_displacement: int
    min_volume_mult: float
    volume_lookback_bars: int
    min_retrace_percent: float
    max_retrace_percent: float
    v3_trail_pips: float

    def __str__(self) -> str:
        return (
            f"{self.pair}: {self.entry_timeframe} "
            f"risk={self.risk_pct}% lookback={self.level_lookback_bars} "
            f"touches>={self.min_touches} disp={self.min_displacement_pips}-"
            f"{self.max_displacement_pips}p retest={self.retest_timeout_bars}bars "
            f"maxSpread={self.max_spread_pips}p"
        )


@dataclass(frozen=True)
class AppConfig:
    # --- cTrader OpenAPI ---
    CTRADER_HOST: str = os.getenv("CTRADER_HOST", "demo.ctraderapi.com")
    CTRADER_PORT: int = int(os.getenv("CTRADER_PORT", 5035))
    CLIENT_ID: str = os.getenv("CLIENT_ID", "")
    CLIENT_SECRET: str = os.getenv("CLIENT_SECRET", "")
    ACCESS_TOKEN: str = os.getenv("ACCESS_TOKEN", "")
    ACCOUNT_ID: int = int(os.getenv("ACCOUNT_ID", 0))

    PAIRS: List[str] = field(
        default_factory=lambda: _parse_pairs(
            os.getenv("PAIRS", ",".join(DEFAULT_PAIRS))
        )
    )

    STRATEGY_TYPE: str = os.getenv("STRATEGY_TYPE", "BREAKOUT_RETEST_V3")

    # --- Движок ---
    PENDING_ORDER_MAX_AGE_SECONDS: int = int(
        os.getenv("PENDING_ORDER_MAX_AGE_SECONDS", "300")
    )
    BREAKOUT_MIN_ORDER_INTERVAL_SECONDS: float = float(
        os.getenv("BREAKOUT_MIN_ORDER_INTERVAL_SECONDS", "0.0")
    )
    PENDING_ORDER_CLEANUP_INTERVAL_SECONDS: int = int(
        os.getenv("PENDING_ORDER_CLEANUP_INTERVAL_SECONDS", "30")
    )
    STRATEGY_DEBUG_EVERY_N: int = int(os.getenv("STRATEGY_DEBUG_EVERY_N", "0"))

    # --- Режим (игнорируется при SCREENER_ONLY=true) ---
    TRADING_MODE: str = os.getenv("TRADING_MODE", "MANUAL")
    SCREENER_ONLY: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("SCREENER_ONLY"), False)
    )
    USE_MULTI_SCANNER: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("USE_MULTI_SCANNER"), True)
    )
    WARMUP_CACHE_ENABLED: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("WARMUP_CACHE_ENABLED"), True)
    )
    PAIR_POLL_DELAY_SEC: float = float(os.getenv("PAIR_POLL_DELAY_SEC", "0"))
    API_METRICS_LOG_INTERVAL_SEC: int = int(
        os.getenv("API_METRICS_LOG_INTERVAL_SEC", "300")
    )
    MAX_LOT: float = float(os.getenv("MAX_LOT", "1.0"))
    TRAILING_STOP_LOSS: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("TRAILING_STOP_LOSS"), True)
    )

    TRADE_WINDOW_START: time = field(
        default_factory=lambda: _parse_time(os.getenv("TRADE_WINDOW_START", "08:00"), time(8, 0))
    )
    TRADE_WINDOW_END: time = field(
        default_factory=lambda: _parse_time(os.getenv("TRADE_WINDOW_END", "16:00"), time(16, 0))
    )

    # --- Бэктест (python main.py --backtest) ---
    BACKTEST_BARS: int = int(os.getenv("BACKTEST_BARS", 26000))
    BACKTEST_WARMUP_BARS: int = int(os.getenv("BACKTEST_WARMUP_BARS", "500"))
    BACKTEST_ASSUMED_SPREAD_PIPS: float = float(os.getenv("BACKTEST_ASSUMED_SPREAD_PIPS", "1.0"))
    BACKTEST_QUIET_LOGS: bool = os.getenv("BACKTEST_QUIET_LOGS", "true").lower() == "true"

    def warmup_bars_for_pair(self, pair: str) -> int:
        """Прогрев M5: lookback + запас для cold_start."""
        cfg = self.get_pair_config(pair)
        return cfg.level_lookback_bars + 50

    def get_pair_config(self, pair: str) -> PairConfig:
        p = pair.upper()

        def _env(key: str) -> str | None:
            return os.getenv(f"{p}_{key}")

        overrides = _BREAKOUT_OVERRIDES.get(p, {})

        def _default(key: str) -> object:
            return overrides.get(key, _BREAKOUT_DEFAULTS[key])

        def _pf(key: str) -> float:
            v = _env(key)
            return float(v) if v is not None else float(_default(key))

        def _pi(key: str) -> int:
            v = _env(key)
            return int(v) if v is not None else int(_default(key))

        def _ps(key: str) -> str:
            v = _env(key)
            return v if v is not None else str(_default(key))

        return PairConfig(
            pair=p,
            entry_timeframe=_ps("BREAKOUT_ENTRY_TIMEFRAME"),
            risk_pct=_pf("BREAKOUT_RISK_PCT"),
            max_spread_pips=_pf("BREAKOUT_MAX_SPREAD_PIPS"),
            level_lookback_bars=_pi("BREAKOUT_LEVEL_LOOKBACK_BARS"),
            min_touches=_pi("BREAKOUT_MIN_TOUCHES"),
            touch_tolerance_pips=_pf("BREAKOUT_TOUCH_TOLERANCE_PIPS"),
            level_cluster_pips=_pf("BREAKOUT_LEVEL_CLUSTER_PIPS"),
            min_level_age_bars=_pi("BREAKOUT_MIN_LEVEL_AGE_BARS"),
            max_level_age_bars=_pi("BREAKOUT_MAX_LEVEL_AGE_BARS"),
            breakout_close_buffer_pips=_pf("BREAKOUT_CLOSE_BUFFER_PIPS"),
            min_body_pips=_pf("BREAKOUT_MIN_BODY_PIPS"),
            max_bars_since_touch=_pi("BREAKOUT_MAX_BARS_SINCE_TOUCH"),
            min_displacement_pips=_pf("BREAKOUT_MIN_DISPLACEMENT_PIPS"),
            max_displacement_pips=_pf("BREAKOUT_MAX_DISPLACEMENT_PIPS"),
            retest_tolerance_pips=_pf("BREAKOUT_RETEST_TOLERANCE_PIPS"),
            retest_timeout_bars=_pi("BREAKOUT_RETEST_TIMEOUT_BARS"),
            sl_buffer_pips=_pf("BREAKOUT_SL_BUFFER_PIPS"),
            max_bars_for_displacement=_pi("BREAKOUT_MAX_BARS_FOR_DISPLACEMENT"),
            min_volume_mult=_pf("BREAKOUT_MIN_VOLUME_MULT"),
            volume_lookback_bars=_pi("BREAKOUT_VOLUME_LOOKBACK_BARS"),
            min_retrace_percent=_pf("BREAKOUT_MIN_RETRACE_PERCENT"),
            max_retrace_percent=_pf("BREAKOUT_MAX_RETRACE_PERCENT"),
            v3_trail_pips=_pf("BREAKOUT_V3_TRAIL_PIPS"),
        )

    def is_breakout_v3(self) -> bool:
        return self.STRATEGY_TYPE.strip().upper() == "BREAKOUT_RETEST_V3"


config = AppConfig()
