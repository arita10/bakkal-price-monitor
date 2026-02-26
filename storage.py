"""
storage.py — Supabase read/write for the v2 schema.

Tables:
  products      — master product registry (one row per URL)
  price_history — daily price time-series (one row per product per day)

Views (read-only, created in schema.sql):
  v_latest_prices — most recent price per product
  v_price_trend   — last 30 days per product
  v_best_deals    — today's biggest drops
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

from parser import ProductData

logger = logging.getLogger("bakkal_monitor.storage")


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

def get_last_price(supabase: Client, product_url: str) -> Optional[float]:
    """
    Return the most recently recorded price for product_url from price_history,
    or None if this is the first time we've seen this product.
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


def get_price_history(
    supabase: Client,
    product_url: str,
    days: int = 30,
) -> list[dict]:
    """
    Return up to `days` daily price records for a product, newest first.
    Uses the price_history table directly.
    """
    try:
        response = (
            supabase.table("price_history")
            .select("current_price, previous_price, price_drop_pct, scraped_date")
            .eq("product_url", product_url)
            .order("scraped_date", desc=True)
            .limit(days)
            .execute()
        )
        return response.data or []
    except Exception as exc:
        logger.error(f"get_price_history error for {product_url}: {exc}")
        return []


def get_best_deals(supabase: Client, limit: int = 10) -> list[dict]:
    """
    Return today's biggest price drops using the v_best_deals view.
    Falls back to price_history query if view doesn't exist yet.
    """
    try:
        response = (
            supabase.table("v_best_deals")
            .select("product_name, market_name, current_price, previous_price, price_drop_pct, product_url")
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception:
        # Fallback: query price_history directly
        try:
            from datetime import date
            today = date.today().isoformat()
            response = (
                supabase.table("price_history")
                .select("product_name, market_name, current_price, previous_price, price_drop_pct, product_url")
                .eq("scraped_date", today)
                .gte("price_drop_pct", 5)
                .order("price_drop_pct", desc=True)
                .limit(limit)
                .execute()
            )
            return response.data or []
        except Exception as exc2:
            logger.error(f"get_best_deals fallback error: {exc2}")
            return []


# ─────────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────────

def upsert_price(
    supabase: Client,
    product: ProductData,
    previous_price: Optional[float],
) -> bool:
    """
    1. Upsert into `products` table (create or update master record).
    2. Upsert into `price_history` table (daily time-series row).

    The UNIQUE constraint on (product_url, scraped_date) means re-runs
    on the same day update the existing row rather than inserting a new one.

    Returns True on success, False on any error.
    """
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.date().isoformat()
    scraped_at_str = now_utc.isoformat()

    price_drop_pct: Optional[float] = None
    if previous_price is not None and previous_price > 0:
        price_drop_pct = round(
            ((previous_price - product.current_price) / previous_price) * 100,
            2,
        )

    # ── 1. Upsert products master table ─────────────────────────────────────
    try:
        (
            supabase.table("products")
            .upsert(
                {
                    "product_url":   product.product_url,
                    "product_name":  product.product_name,
                    "market_name":   product.market_name,
                    "latest_price":  product.current_price,
                    "last_seen_at":  scraped_at_str,
                },
                on_conflict="product_url",
            )
            .execute()
        )
    except Exception as exc:
        # Non-fatal: products table may not exist yet on first deploy
        logger.warning(f"products upsert skipped for {product.product_name!r}: {exc}")

    # ── 2. Upsert price_history time-series ─────────────────────────────────
    record = {
        "product_url":    product.product_url,
        "product_name":   product.product_name,
        "market_name":    product.market_name,
        "current_price":  product.current_price,
        "previous_price": previous_price,
        "price_drop_pct": price_drop_pct,
        "scraped_date":   today_str,
        "scraped_at":     scraped_at_str,
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
            + (f" (drop {price_drop_pct:+.1f}%)" if price_drop_pct else "")
        )
        return True
    except Exception as exc:
        logger.error(f"upsert_price error for {product.product_name!r}: {exc}")
        return False
