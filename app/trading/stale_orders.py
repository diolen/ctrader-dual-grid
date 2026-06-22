"""
Отмена зависших pending limit-ордеров у брокера.
"""

import logging
import time
from typing import Optional, Set

from app.config.settings import config

logger = logging.getLogger(__name__)


def pending_order_max_age_seconds() -> float:
    return float(config.PENDING_ORDER_MAX_AGE_SECONDS)


def _order_age_seconds(order: dict, now_ms: int) -> float:
    ts = order.get("last_update_ms") or order.get("open_timestamp_ms") or 0
    if not ts:
        return 0.0
    return max(0.0, (now_ms - int(ts)) / 1000.0)


def _tracked_client_order_ids(orchestrator) -> Set[str]:
    ids: Set[str] = set()
    tracked = getattr(orchestrator, "get_tracked_limit_orders", lambda: {})()
    for info in tracked.values():
        cid = info.get("client_order_id") or ""
        if cid:
            ids.add(cid)
    return ids


async def cancel_stale_pending_orders(
    client,
    orchestrator,
    max_age_sec: Optional[float] = None,
    skip_client_order_ids: Optional[Set[str]] = None,
    pending_orders: Optional[list] = None,
) -> int:
    """
    Отменяет у брокера pending-ордера старше TTL и снимает блокировки.
    Не трогает clientOrderId из skip_client_order_ids и текущих tracked-лимиток.
    """
    now_ms = int(time.time() * 1000)
    cancelled = 0

    skip = set(skip_client_order_ids or ())
    skip |= _tracked_client_order_ids(orchestrator)

    try:
        if pending_orders is None:
            pending_orders = await client.get_pending_orders()
    except Exception as e:
        logger.error(f"❌ Не удалось получить pending-ордера: {e}", exc_info=True)
        return 0

    for order in pending_orders:
        pair = order.get("pair") or ""
        cid = order.get("clientOrderId") or ""
        if cid and cid in skip:
            continue

        broker_id = order.get("orderId")
        if not broker_id:
            continue

        max_age = max_age_sec if max_age_sec is not None else pending_order_max_age_seconds()
        age = _order_age_seconds(order, now_ms)
        if age < max_age:
            continue

        logger.warning(
            f"⏰ [{pair or '?'}] Отмена зависшего ордера #{broker_id} "
            f"(возраст {age:.0f}s > {max_age:.0f}s)"
        )
        try:
            ok = await client.cancel_order(int(broker_id))
        except Exception as e:
            logger.error(f"❌ [{pair}] Ошибка отмены ордера #{broker_id}: {e}", exc_info=True)
            ok = False

        if ok:
            cancelled += 1
            await orchestrator.release_pair_blocks(pair, cid or None)

    return cancelled
