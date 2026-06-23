"""Local backtest module - run backtests with locally saved candle data."""

import logging
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.models.candle import Candle
from app.strategy.backtest import run_backtest_v3
from app.config.settings import config


logger = logging.getLogger(__name__)


def load_candles_from_csv(
    file_path: str,
    timestamp_col: str = "timestamp",
    open_col: str = "open",
    high_col: str = "high",
    low_col: str = "low",
    close_col: str = "close",
    volume_col: str = "volume",
) -> List[Candle]:
    """
    Load candles from CSV file.
    
    Expected CSV format:
    timestamp,open,high,low,close,volume
    2024-01-01 00:00:00,1.1000,1.1010,1.0990,1.1005,1000
    OR Unix timestamp:
    1773619200,1.14416,1.14456,1.14416,1.14443,0
    
    Args:
        file_path: Path to CSV file
        timestamp_col: Name of timestamp column
        open_col: Name of open column
        high_col: Name of high column
        low_col: Name of low column
        close_col: Name of close column
        volume_col: Name of volume column
    
    Returns:
        List of Candle objects
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {file_path}")
    
    df = pd.read_csv(file_path)
    
    candles = []
    for _, row in df.iterrows():
        # Try to parse timestamp - supports both Unix timestamp and datetime string
        ts_value = row[timestamp_col]
        try:
            # Try Unix timestamp first
            timestamp = datetime.utcfromtimestamp(float(ts_value))
        except (ValueError, TypeError):
            # Fall back to datetime string
            timestamp = pd.to_datetime(ts_value)
        
        candle = Candle(
            timestamp=timestamp,
            open=float(row[open_col]),
            high=float(row[high_col]),
            low=float(row[low_col]),
            close=float(row[close_col]),
            volume=int(row[volume_col]),
        )
        candles.append(candle)
    
    logger.info(f"Loaded {len(candles)} candles from {file_path}")
    return candles


async def run_local_backtest(
    pair: str,
    candles: List[Candle],
    digits: int,
    pip_value: float,
    timeframe: str = "M5",
    trail_pips: float = 5.0,
) -> None:
    """
    Run backtest with local candle data.
    
    Args:
        pair: Trading pair symbol (e.g., EURUSD)
        candles: List of Candle objects
        digits: Number of decimal places for price
        pip_value: Pip value for the pair
        timeframe: Timeframe (e.g., M5)
        trail_pips: Trailing stop in pips
    """
    from app.config.settings import config
    
    pair_cfg = config.get_pair_config(pair)
    
    logger.info(
        f"🧪 Local Backtest {pair} ({timeframe}) | {len(candles)} candles | {pair_cfg}"
    )
    
    # Run backtest
    await run_backtest_v3(
        candles,
        digits,
        timeframe=timeframe,
        pair=pair,
        pip_value=pip_value,
        pair_config=pair_cfg,
        trail_pips=trail_pips,
    )


async def run_local_backtest_from_csv(
    csv_file: str,
    pair: str,
    digits: int,
    pip_value: float,
    timeframe: str = "M5",
    trail_pips: float = 5.0,
) -> None:
    """
    Run backtest from CSV file.
    
    Args:
        csv_file: Path to CSV file with candle data
        pair: Trading pair symbol (e.g., EURUSD)
        digits: Number of decimal places for price
        pip_value: Pip value for the pair
        timeframe: Timeframe (e.g., M5)
        trail_pips: Trailing stop in pips
    """
    candles = load_candles_from_csv(csv_file)
    await run_local_backtest(pair, candles, digits, pip_value, timeframe, trail_pips)


def save_candles_to_csv(
    candles: List[Candle],
    file_path: str,
) -> None:
    """
    Save candles to CSV file.
    
    Args:
        candles: List of Candle objects
        file_path: Path to save CSV file
    """
    data = []
    for candle in candles:
        # Handle both datetime and int timestamp
        if isinstance(candle.timestamp, int):
            # Check if timestamp is in milliseconds (year > 3000 indicates milliseconds)
            ts = candle.timestamp
            if ts > 10000000000:  # Milliseconds
                ts = ts / 1000
            timestamp = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        else:
            timestamp = candle.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        
        data.append({
            "timestamp": timestamp,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        })
    
    df = pd.DataFrame(data)
    df.to_csv(file_path, index=False)
    logger.info(f"Saved {len(candles)} candles to {file_path}")
