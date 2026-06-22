"""Technical indicator helpers for MarketContextBuilder."""
from __future__ import annotations

import pandas as pd


def compute_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    high = candles["high"]
    low = candles["low"]
    close = candles["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def detect_swing_points(
    candles: pd.DataFrame,
    *,
    lookback: int = 80,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """
    Swing highs/lows via vectorized neighbour comparison.
    Returns ([(index, price), ...], [(index, price), ...]).
    """
    n = len(candles)
    if n < 5:
        return [], []

    start = max(1, n - lookback)
    end = n - 1
    highs = candles["high"].values
    lows = candles["low"].values

    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    for i in range(start + 1, end):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            swing_highs.append((i, float(highs[i])))
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            swing_lows.append((i, float(lows[i])))

    return swing_highs, swing_lows


def cluster_prices(
    prices: list[float],
    tolerance: float,
) -> list[float]:
    """Cluster prices within tolerance; return cluster centroids."""
    if not prices:
        return []
    sorted_prices = sorted(prices)
    clusters: list[list[float]] = [[sorted_prices[0]]]
    for price in sorted_prices[1:]:
        if abs(price - clusters[-1][-1]) <= tolerance:
            clusters[-1].append(price)
        else:
            clusters.append([price])
    return [sum(c) / len(c) for c in clusters]


def cluster_liquidity_zones(
    swing_highs: list[float],
    swing_lows: list[float],
    atr: float,
) -> tuple[tuple[float, str], ...]:
    """Equal highs/lows within 0.5 × ATR → liquidity zones."""
    tol = 0.5 * atr
    zones: list[tuple[float, str]] = []

    def _cluster_side(values: list[float], zone_type: str) -> None:
        if len(values) < 2:
            return
        sorted_vals = sorted(values)
        cluster: list[float] = [sorted_vals[0]]
        for val in sorted_vals[1:]:
            if abs(val - cluster[-1]) <= tol:
                cluster.append(val)
            else:
                if len(cluster) >= 2:
                    zones.append((sum(cluster) / len(cluster), zone_type))
                cluster = [val]
        if len(cluster) >= 2:
            zones.append((sum(cluster) / len(cluster), zone_type))

    _cluster_side(swing_highs, "SELL_SIDE")
    _cluster_side(swing_lows, "BUY_SIDE")
    return tuple(zones)
