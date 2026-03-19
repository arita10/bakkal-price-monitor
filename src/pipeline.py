"""
src/pipeline.py — Data cleaning and Aiven PostgreSQL storage.

Handles:
  get_last_prices()   — bulk-fetch last prices for all products in ONE query
  get_price_history() — read N days of history for a product
  get_best_deals()    — today's biggest price drops
  upsert_prices()     — bulk-write products + price_history in chunks
  upsert_price()      — single-product write (kept for backward compat)
"""

import logging
from datetime import datetime, timezone, date
from typing import Optional

import psycopg2
import psycopg2.extras

from src.agents.parser import ProductData

logger = logging.getLogger("bakkal_monitor.pipeline")

_BATCH_SIZE = 500   # max rows per upsert call


def _connect(db_url: str) -> psycopg2.extensions.connection:
    """Return a psycopg2 connection to Aiven PostgreSQL."""
    return psycopg2.connect(db_url)


# ─────────────────────────────────────────────────────────────────────────────
# Reads
# ─────────────────────────────────────────────────────────────────────────────

def get_last_prices(db_url: str, product_urls: list[str]) -> dict[str, float]:
    """
    Bulk-fetch the most recent price for every URL in one query.
    Returns {product_url: price}. Missing URLs → not in dict.
    """
    result: dict[str, float] = {}
    if not product_urls:
        return result

    try:
        conn = _connect(db_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        # DISTINCT ON gives the latest row per product_url
        cur.execute(
            """
            SELECT DISTINCT ON (product_url) product_url, current_price
            FROM sp_price_history
            WHERE product_url = ANY(%s)
            ORDER BY product_url, scraped_at DESC
            """,
            (product_urls,),
        )
        for row in cur.fetchall():
            result[row["product_url"]] = float(row["current_price"])

        cur.close()
        conn.close()
    except Exception as exc:
        logger.error(f"get_last_prices error: {exc}")

    return result


def get_last_price(db_url: str, product_url: str) -> Optional[float]:
    """Single-product lookup (backward compat). Prefer get_last_prices() in bulk."""
    prices = get_last_prices(db_url, [product_url])
    return prices.get(product_url)


def get_price_history(
    db_url: str,
    product_url: str,
    days: int = 30,
) -> list[dict]:
    """Return up to `days` daily price records for a product, newest first."""
    try:
        conn = _connect(db_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            """
            SELECT current_price, previous_price, price_drop_pct, scraped_date
            FROM sp_price_history
            WHERE product_url = %s
            ORDER BY scraped_date DESC
            LIMIT %s
            """,
            (product_url, days),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as exc:
        logger.error(f"get_price_history error for {product_url}: {exc}")
        return []


def get_best_deals(db_url: str, limit: int = 10) -> list[dict]:
    """Return today's biggest price drops."""
    try:
        conn = _connect(db_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            """
            SELECT product_name, market_name, current_price,
                   previous_price, price_drop_pct, product_url
            FROM sp_v_best_deals
            LIMIT %s
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as exc:
        logger.error(f"get_best_deals error: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Writes
# ─────────────────────────────────────────────────────────────────────────────

def upsert_prices(
    db_url: str,
    products: list[ProductData],
    last_prices: dict[str, float],
) -> tuple[int, int]:
    """
    Bulk-upsert all products in batches of _BATCH_SIZE.
    Returns (success_count, error_count).
    """
    now_utc    = datetime.now(timezone.utc)
    today_str  = now_utc.date().isoformat()
    scraped_at = now_utc.isoformat()
    success = 0
    errors  = 0

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

        product_rows.append((
            p.product_url,
            p.product_name,
            p.market_name,
            p.current_price,
            scraped_at,
        ))
        history_rows.append((
            p.product_url,
            p.product_name,
            p.market_name,
            p.current_price,
            prev,
            drop_pct,
            today_str,
            scraped_at,
        ))

    try:
        conn = _connect(db_url)
        cur = conn.cursor()

        # ── Batch upsert products table ───────────────────────────────────────
        for i in range(0, len(product_rows), _BATCH_SIZE):
            chunk = product_rows[i : i + _BATCH_SIZE]
            try:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO sp_products (product_url, product_name, market_name, latest_price, last_seen_at)
                    VALUES %s
                    ON CONFLICT (product_url) DO UPDATE SET
                        product_name  = EXCLUDED.product_name,
                        market_name   = EXCLUDED.market_name,
                        latest_price  = EXCLUDED.latest_price,
                        last_seen_at  = EXCLUDED.last_seen_at
                    """,
                    chunk,
                )
                conn.commit()
                logger.debug(f"products batch {i // _BATCH_SIZE + 1}: {len(chunk)} rows")
            except Exception as exc:
                conn.rollback()
                logger.error(f"products batch upsert error: {exc}")
                errors += len(chunk)

        # ── Batch upsert price_history table ─────────────────────────────────
        for i in range(0, len(history_rows), _BATCH_SIZE):
            chunk = history_rows[i : i + _BATCH_SIZE]
            try:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO sp_price_history
                        (product_url, product_name, market_name, current_price,
                         previous_price, price_drop_pct, scraped_date, scraped_at)
                    VALUES %s
                    ON CONFLICT (product_url, scraped_date) DO UPDATE SET
                        current_price  = EXCLUDED.current_price,
                        previous_price = EXCLUDED.previous_price,
                        price_drop_pct = EXCLUDED.price_drop_pct,
                        scraped_at     = EXCLUDED.scraped_at
                    """,
                    chunk,
                )
                conn.commit()
                success += len(chunk)
                logger.debug(f"price_history batch {i // _BATCH_SIZE + 1}: {len(chunk)} rows")
            except Exception as exc:
                conn.rollback()
                logger.error(f"price_history batch upsert error: {exc}")
                errors += len(chunk)

        cur.close()
        conn.close()
    except Exception as exc:
        logger.error(f"DB connection error in upsert_prices: {exc}")
        errors += len(product_rows)

    logger.info(f"Bulk upsert complete: {success} ok, {errors} errors")
    return success, errors


def upsert_price(
    db_url: str,
    product: ProductData,
    previous_price: Optional[float],
) -> bool:
    """Single-product upsert (backward compat). Prefer upsert_prices() in bulk."""
    ok, _ = upsert_prices(
        db_url,
        [product],
        {product.product_url: previous_price} if previous_price else {},
    )
    return ok == 1
