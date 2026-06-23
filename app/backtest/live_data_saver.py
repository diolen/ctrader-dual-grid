"""Live data saver - save live candles to CSV for backtesting."""

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from app.models.candle import Candle
from app.backtest.local_backtest import save_candles_to_csv


logger = logging.getLogger(__name__)


class LiveDataSaver:
    """Save live candles to CSV files for backtesting."""
    
    def __init__(self, output_dir: str = "data"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
    
    def get_csv_path(self, pair: str, timeframe: str) -> Path:
        """Generate CSV file path for a pair and timeframe."""
        filename = f"{pair}_{timeframe}.csv"
        return self.output_dir / filename
    
    def save_candles(
        self,
        pair: str,
        candles: List[Candle],
        timeframe: str = "M5",
        append: bool = True,
    ) -> None:
        """
        Save candles to CSV file.
        
        Args:
            pair: Trading pair symbol (e.g., EURUSD)
            candles: List of Candle objects
            timeframe: Timeframe (e.g., M5)
            append: If True, append to existing file; if False, overwrite
        """
        csv_path = self.get_csv_path(pair, timeframe)
        
        if append and csv_path.exists():
            # Load existing candles and merge
            existing_candles = self._load_existing_candles(csv_path)
            # Remove duplicates based on timestamp
            existing_timestamps = {c.timestamp for c in existing_candles}
            new_candles = [c for c in candles if c.timestamp not in existing_timestamps]
            
            if new_candles:
                all_candles = existing_candles + new_candles
                all_candles.sort(key=lambda x: x.timestamp)
                save_candles_to_csv(all_candles, str(csv_path))
                logger.info(f"Appended {len(new_candles)} new candles to {csv_path}")
            else:
                logger.info(f"No new candles to append to {csv_path}")
        else:
            save_candles_to_csv(candles, str(csv_path))
            logger.info(f"Saved {len(candles)} candles to {csv_path}")
    
    def _load_existing_candles(self, csv_path: Path) -> List[Candle]:
        """Load existing candles from CSV file."""
        from app.backtest.local_backtest import load_candles_from_csv
        return load_candles_from_csv(str(csv_path))
    
    def save_single_candle(
        self,
        pair: str,
        candle: Candle,
        timeframe: str = "M5",
    ) -> None:
        """
        Save a single candle to CSV file (append mode).
        
        Args:
            pair: Trading pair symbol (e.g., EURUSD)
            candle: Candle object to save
            timeframe: Timeframe (e.g., M5)
        """
        self.save_candles(pair, [candle], timeframe, append=True)


async def save_live_data_from_api(
    client,
    pair: str,
    symbol_id: int,
    timeframe: str = "M5",
    bars: int = 1000,
    output_dir: str = "data",
) -> None:
    """
    Fetch live data from API and save to CSV.
    
    Args:
        client: CTraderClient instance
        pair: Trading pair symbol (e.g., EURUSD)
        symbol_id: Symbol ID from cTrader
        timeframe: Timeframe (e.g., M5)
        bars: Number of bars to fetch
        output_dir: Output directory for CSV files
    """
    import ctrader_open_api.messages.OpenApiModelMessages_pb2 as model_proto
    
    # Timeframe mappings (copied from main.py)
    TIMEFRAME_MAP = {
        "M1": model_proto.M1,
        "M2": model_proto.M2,
        "M3": model_proto.M3,
        "M4": model_proto.M4,
        "M5": model_proto.M5,
        "M10": model_proto.M10,
        "M15": model_proto.M15,
        "M30": model_proto.M30,
        "H1": model_proto.H1,
        "H4": model_proto.H4,
        "D1": model_proto.D1,
        "W1": model_proto.W1,
    }
    
    TIMEFRAME_MINUTES = {
        "M1": 1,
        "M2": 2,
        "M3": 3,
        "M4": 4,
        "M5": 5,
        "M10": 10,
        "M15": 15,
        "M30": 30,
        "H1": 60,
        "H4": 240,
        "D1": 1440,
        "W1": 10080,
    }
    
    period = TIMEFRAME_MAP.get(timeframe, model_proto.M5)
    entry_minutes = TIMEFRAME_MINUTES.get(timeframe, 5)
    bar_ms = entry_minutes * 60 * 1000
    
    now = int(datetime.now().timestamp())
    from_ts = now - bars * entry_minutes * 60
    
    logger.info(f"Fetching {bars} bars for {pair} ({timeframe}) from API...")
    
    candles = await client.get_trendbars_chunked(
        symbol_id,
        period,
        from_ts * 1000,
        now * 1000,
        bar_ms=bar_ms,
        pair=pair,
        timeframe=timeframe,
    )
    
    saver = LiveDataSaver(output_dir)
    saver.save_candles(pair, candles, timeframe, append=False)
    
    logger.info(f"Successfully saved {len(candles)} candles to {saver.get_csv_path(pair, timeframe)}")
