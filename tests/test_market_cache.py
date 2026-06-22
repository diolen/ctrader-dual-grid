# test_market_cache.py
import asyncio
import math
from app.connection.market_cache import MarketCache

async def test_market_cache():
    cache = MarketCache()
    await cache.update_quote('EURUSD', 1.0800, 1.0802, volume=100)
    
    # 🔥 SYNC read: no await needed
    metrics = cache.get_metrics('EURUSD')
    assert metrics is not None, '❌ No metrics'
    
    # 🔥 Используем округление или isclose для сравнения
    assert math.isclose(metrics.spread, 2.0, rel_tol=1e-6), f'❌ Spread: {metrics.spread}'
    assert math.isclose(cache.get_spread('EURUSD'), 2.0, rel_tol=1e-6), '❌ get_spread failed'
    
    # 🔥 Stale check
    assert not cache.is_pair_stale('EURUSD'), '❌ False stale'
    
    # JPY-пары: pip=0.01
    await cache.update_quote('EURJPY', 150.00, 150.02, volume=50)
    jpy_metrics = cache.get_metrics('EURJPY')
    assert math.isclose(jpy_metrics.spread, 2.0, rel_tol=1e-6), f'❌ JPY spread: {jpy_metrics.spread}'
    
    print('✅ MarketCache: all sync operations work correctly')
    print(f'   - EURUSD spread: {metrics.spread:.1f} pips')
    print(f'   - EURJPY spread: {jpy_metrics.spread:.1f} pips')

    # Частичный SpotEvent: только ask, затем только bid
    await cache.update_quote('EURUSD', bid=1.0800, ask=1.0802, pip_value=0.0001)
    await cache.update_quote('EURUSD', ask=1.0803)
    m = cache.get_metrics('EURUSD')
    assert m is not None and math.isclose(m.ask, 1.0803)
    assert math.isclose(m.bid, 1.0800)
    await cache.update_quote('EURUSD', bid=0.0, ask=1.0804)  # bid=0 → сохранить предыдущий
    m2 = cache.get_metrics('EURUSD')
    assert math.isclose(m2.bid, 1.0800) and math.isclose(m2.ask, 1.0804)
    print('✅ MarketCache: partial spot merge OK')

if __name__ == '__main__':
    asyncio.run(test_market_cache())