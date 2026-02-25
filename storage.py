"""
storage.py — Supabase read/write operations for price_history table.

get_last_price()  — fetch most recent price for a product URL
upsert_price()    — insert or update today's price record
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

from parser import ProductData

logger = logging.getLogger("bakkal_monitor.storage")


def get_last_price(supabase: Client, product_url: str) -> Optional[float]:
    """
    Return the most recently recorded price for product_url, or None
    if this is the first time we've seen this product.
    """
    try:
        response = (
            supabase.table("price_history")
            .select("current_price")
            .eq("product_url", product_url)
            .order("scraped_at", desc=True)
            .limit(1)
            .execute()
        )
        if response.data:
            return float(response.data[0]["current_price"])
        return None
    except Exception as exc:
        logger.error(f"get_last_price error for {product_url}: {exc}")
        return None


def upsert_price(
    supabase: Client,
    product: ProductData,
    previous_price: Optional[float],
) -> bool:
    """
    Insert or update today's price record for product_url.
    The UNIQUE constraint on (product_url, scraped_date) means re-runs
    on the same day will update the existing row instead of inserting.

    Returns True on success, False on error.
    """
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.date().isoformat()           # e.g. "2026-02-25"
    scraped_at_str = now_utc.isoformat()

    price_drop_pct: Optional[float] = None
    if previous_price is not None and previous_price > 0:
        price_drop_pct = round(
            ((previous_price - product.current_price) / previous_price) * 100,
            2,
        )

    record = {
        "product_url": product.product_url,
        "product_name": product.product_name,
        "market_name": product.market_name,
        "current_price": product.current_price,
        "previous_price": previous_price,
        "price_drop_pct": price_drop_pct,
        "scraped_date": today_str,
        "scraped_at": scraped_at_str,
    }

    try:
        (
            supabase.table("price_history")
            .upsert(record, on_conflict="product_url,scraped_date")
            .execute()
        )
        logger.info(
            f"Upserted: {product.product_name!r} @ {product.current_price:.2f} TL "
            f"[{product.market_name}]"
        )
        return True
    except Exception as exc:
        logger.error(f"upsert_price error for {product.product_name!r}: {exc}")
        return False
