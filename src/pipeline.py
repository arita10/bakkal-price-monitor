"""
src/pipeline.py — Data cleaning and Supabase storage.

Handles:
  get_last_prices()   — bulk-fetch last prices for all products in ONE query
  get_price_history() — read N days of history for a product
  get_best_deals()    — today's biggest price drops
  upsert_prices()     — bulk-write products + price_history in chunks
  upsert_price()      — single-product write (kept for backward compat)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import Client

from src.agents.parser import ProductData

logger = logging.getLogger("bakkal_monitor.pipeline")

_BATCH_SIZE = 500   # max rows per Supabase upsert call


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

def get_last_prices(supabase: Client, product_urls: list[str]) -> dict[str, float]:
    """
    Bulk-fetch the most recent price for every URL in one query.
    Returns {product_url: price}. Missing URLs → not in dict.

    Uses a single SELECT with .in_() instead of one query per product.
    """
    result: dict[str, float] = {}
    if not product_urls:
        return result

    # Fetch in chunks to avoid URL-length limits (~2000 URLs safe per call)
    for i in range(0, len(product_urls), 2000):
        chunk = product_urls[i : i + 2000]
        try:
            response = (
                supabase.table("price_history")
                .select("product_url, current_price, scraped_at")
                .in_("product_url", chunk)
                .order("scraped_at", desc=True)
                .execute()
            )
            # Keep only the most-recent row per URL
            for row in response.data or []:
                url = row["product_url"]
                if url not in result:
                    result[url] = float(row["current_price"])
        except Exception as exc:
            logger.error(f"get_last_prices chunk error: {exc}")

    return result


def get_last_price(supabase: Client, product_url: str) -> Optional[float]:
    """Single-product lookup (backward compat). Prefer get_last_prices() in bulk."""
    prices = get_last_prices(supabase, [product_url])
    return prices.get(product_url)


def get_price_history(
    supabase: Client,
    product_url: str,
    days: int = 30,
) -> list[dict]:
    """Return up to `days` daily price records for a product, newest first."""
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
    """Return today's biggest price drops (uses v_best_deals view if available)."""
    try:
        response = (
            supabase.table("v_best_deals")
            .select("product_name, market_name, current_price, previous_price, price_drop_pct, product_url")
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception:
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

def upsert_prices(
    supabase: Client,
    products: list[ProductData],
    last_prices: dict[str, float],
) -> tuple[int, int]:
    """
    Bulk-upsert all products in batches of _BATCH_SIZE.

    Returns (success_count, error_count).
    Uses two batch calls (products table + price_history table)
    instead of 2 × N individual calls.
    """
    now_utc       = datetime.now(timezone.utc)
    today_str     = now_utc.date().isoformat()
    scraped_at    = now_utc.isoformat()
    success = 0
    errors  = 0

    # Build record lists
    product_rows = []
    history_rows = []

    for p in products:
        if not p.product_url or p.current_price <= 0:
            errors += 1
            continue

        prev = last_prices.get(p.product_url)
        drop_pct: Optional[float] = None
        if prev is not None and prev > 0:
            drop_pct = round(((prev - p.current_price) / prev) * 100, 2)

        product_rows.append({
            "product_url":  p.product_url,
            "product_name": p.product_name,
            "market_name":  p.market_name,
            "latest_price": p.current_price,
            "last_seen_at": scraped_at,
        })
        history_rows.append({
            "product_url":    p.product_url,
            "product_name":   p.product_name,
            "market_name":    p.market_name,
            "current_price":  p.current_price,
            "previous_price": prev,
            "price_drop_pct": drop_pct,
            "scraped_date":   today_str,
            "scraped_at":     scraped_at,
        })

    # ── Batch upsert products table ───────────────────────────────────────────
    for i in range(0, len(product_rows), _BATCH_SIZE):
        chunk = product_rows[i : i + _BATCH_SIZE]
        try:
            supabase.table("products").upsert(chunk, on_conflict="product_url").execute()
            logger.debug(f"products batch {i//  _BATCH_SIZE + 1}: {len(chunk)} rows")
        except Exception as exc:
            logger.error(f"products batch upsert error: {exc}")
            errors += len(chunk)

    # ── Batch upsert price_history table ─────────────────────────────────────
    for i in range(0, len(history_rows), _BATCH_SIZE):
        chunk = history_rows[i : i + _BATCH_SIZE]
        try:
            supabase.table("price_history").upsert(
                chunk, on_conflict="product_url,scraped_date"
            ).execute()
            success += len(chunk)
            logger.debug(f"price_history batch {i // _BATCH_SIZE + 1}: {len(chunk)} rows")
        except Exception as exc:
            logger.error(f"price_history batch upsert error: {exc}")
            errors += len(chunk)

    logger.info(f"Bulk upsert complete: {success} ok, {errors} errors")
    return success, errors


def upsert_price(
    supabase: Client,
    product: ProductData,
    previous_price: Optional[float],
) -> bool:
    """Single-product upsert (backward compat). Prefer upsert_prices() in bulk."""
    ok, _ = upsert_prices(supabase, [product], {product.product_url: previous_price} if previous_price else {})
    return ok == 1
