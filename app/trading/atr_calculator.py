"""ATR calculation utility for Trading Layer."""

import pandas as pd
import numpy as np
from typing import List


def compute_atr(candles_df: pd.DataFrame, period: int = 14) -> float:
    """
    Calculate ATR using Wilder's smoothing method.
    
    Args:
        candles_df: DataFrame with columns: timestamp, open, high, low, close, volume
        period: ATR period (default 14)
    
    Returns:
        Current ATR value
    """
    if len(candles_df) < period + 1:
        return 0.0
    
    df = candles_df.copy()
    
    # Calculate True Range
    df['prev_close'] = df['close'].shift(1)
    df['tr1'] = df['high'] - df['low']
    df['tr2'] = abs(df['high'] - df['prev_close'])
    df['tr3'] = abs(df['low'] - df['prev_close'])
    df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    
    # Wilder's smoothing
    # First ATR value is simple average of first 'period' TR values
    first_atr = df['tr'].iloc[1:period+1].mean()
    
    # Subsequent values use exponential smoothing
    atr_values = [first_atr]
    for i in range(period + 1, len(df)):
        atr = (atr_values[-1] * (period - 1) + df['tr'].iloc[i]) / period
        atr_values.append(atr)
    
    return atr_values[-1] if atr_values else 0.0


def compute_atr_baseline(atr_history: List[float]) -> float:
    """
    Calculate ATR baseline as mean of history.
    
    Args:
        atr_history: List of ATR values
    
    Returns:
        Mean ATR value (0.0 if history is empty)
    """
    if not atr_history:
        return 0.0
    return sum(atr_history) / len(atr_history)
