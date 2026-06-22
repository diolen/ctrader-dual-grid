# test_guard.py
import asyncio
from app.strategy.trade_guard import TradeGuard


async def test_trade_guard():
    g = TradeGuard()
    pair = "EURUSD"

    assert await g.can_trade(pair)

    await g.mark_order_pending("ord1", pair)
    assert not await g.can_trade(pair)

    await g.mark_order_complete("ord1", pair)
    await asyncio.sleep(1.1)
    assert await g.can_trade(pair)


if __name__ == "__main__":
    asyncio.run(test_trade_guard())
    print("✅ TradeGuard: pending-only")
