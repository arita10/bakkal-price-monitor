"""
src/enrichment.py — Product name → barcode enrichment via Open Food Facts.

Flow per product name:
  1. Check sp_product_name_map — already mapped? return barcode immediately.
  2. Query Open Food Facts API by name → pick best match (score >= threshold).
  3. Upsert into sp_product_catalog (barcode, canonical_name, brand, …).
  4. Insert into sp_product_name_map (scraped_name → barcode, score, method).

Called after every scrape run with the full list of unique products.
"""

import logging
import time
from typing import Optional

import httpx
from rapidfuzz import fuzz
from supabase import Client

logger = logging.getLogger("bakkal_monitor.enrichment")

_OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
_SCORE_THRESHOLD = 60      # minimum fuzzy score to accept a match
_OFF_DELAY       = 0.5     # seconds between OFF API calls (be polite)


# ─────────────────────────────────────────────────────────────────────────────
# Open Food Facts helpers
# ─────────────────────────────────────────────────────────────────────────────

def _query_off(name: str) -> Optional[dict]:
    """
    Search Open Food Facts by product name.
    Returns the best-matching product dict or None.
    """
    try:
        resp = httpx.get(
            _OFF_SEARCH_URL,
            params={
                "search_terms": name,
                "search_simple": 1,
                "action":        "process",
                "json":          1,
                "page_size":     5,
                "lc":            "tr",   # prefer Turkish results
            },
            timeout=10,
            headers={"User-Agent": "bakkal-price-monitor/1.0 (contact: github.com/arita10)"},
        )
        resp.raise_for_status()
        products = resp.json().get("products", [])
        if not products:
            return None

        # Pick the product whose product_name best matches our scraped name
        best       = None
        best_score = 0
        for p in products:
            off_name = p.get("product_name") or p.get("product_name_tr") or ""
            if not off_name:
                continue
            score = fuzz.token_set_ratio(name.upper(), off_name.upper())
            if score > best_score:
                best_score = score
                best = p
                best["_match_score"] = score

        if best and best_score >= _SCORE_THRESHOLD:
            return best
        return None

    except Exception as exc:
        logger.debug(f"OFF API error for '{name}': {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Supabase helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_existing_map(sb: Client, names: list[str]) -> dict[str, str]:
    """
    Bulk-fetch already-mapped names from sp_product_name_map.
    Returns {scraped_name: barcode}.
    """
    result = {}
    if not names:
        return result
    try:
        for i in range(0, len(names), 50):   # small chunks — long names = huge URLs
            chunk = names[i:i + 50]
            resp = (
                sb.table("sp_product_name_map")
                .select("scraped_name, barcode")
                .in_("scraped_name", chunk)
                .not_.is_("barcode", "null")
                .execute()
            )
            for row in (resp.data or []):
                result[row["scraped_name"]] = row["barcode"]
    except Exception as exc:
        logger.error(f"get_existing_map error: {exc}")
    return result


def _upsert_catalog(sb: Client, product: dict, barcode: str) -> None:
    """Insert/update sp_product_catalog row."""
    try:
        sb.table("sp_product_catalog").upsert({
            "barcode":        barcode,
            "canonical_name": (product.get("product_name") or product.get("product_name_tr") or "").strip(),
            "brand":          (product.get("brands") or "").strip() or None,
            "product_weight": (product.get("quantity") or "").strip() or None,
            "category":       (product.get("categories_tags") or [""])[0].replace("en:", "").replace("tr:", "") or None,
            "image_url":      product.get("image_front_small_url") or product.get("image_url") or None,
            "off_data":       {
                k: product[k] for k in
                ("product_name", "brands", "quantity", "categories_tags", "nutriments")
                if k in product
            },
            "updated_at":     "now()",
        }, on_conflict="barcode").execute()
    except Exception as exc:
        logger.debug(f"upsert_catalog error for barcode {barcode}: {exc}")


def _upsert_name_map(
    sb: Client,
    scraped_name: str,
    market_name: str,
    barcode: Optional[str],
    score: float,
    method: str,
) -> None:
    """Insert/update sp_product_name_map row."""
    try:
        sb.table("sp_product_name_map").upsert({
            "scraped_name":  scraped_name,
            "market_name":   market_name,
            "barcode":       barcode,
            "match_score":   round(score, 2),
            "match_method":  method,
            "verified":      False,
        }, on_conflict="scraped_name,market_name").execute()
    except Exception as exc:
        logger.debug(f"upsert_name_map error for '{scraped_name}': {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Main enrichment function
# ─────────────────────────────────────────────────────────────────────────────

def enrich_products(sb: Client, products: list) -> dict[str, str]:
    """
    For each product in `products`, attempt to find a barcode via:
      1. Existing sp_product_name_map cache
      2. Open Food Facts API (if not cached)

    Saves results to sp_product_catalog + sp_product_name_map.
    Returns {scraped_name: barcode} for all successfully matched products.
    """
    if not products:
        return {}

    names = list({p.product_name for p in products if p.product_name})
    logger.info(f"Enrichment: {len(names)} unique product names to process")

    # ── Step 1: bulk-fetch already-mapped names ───────────────────────────────
    existing = _get_existing_map(sb, names)
    logger.info(f"Enrichment: {len(existing)} already mapped in cache")

    unmapped = [n for n in names if n not in existing]
    logger.info(f"Enrichment: {len(unmapped)} names need OFF lookup")

    # ── Step 2: query OFF for unmapped names ──────────────────────────────────
    new_mappings: dict[str, str] = {}
    no_match_count = 0

    # Build market_name lookup for unmapped names
    name_to_market = {p.product_name: p.market_name for p in products}

    for i, name in enumerate(unmapped, 1):
        off_product = _query_off(name)
        market      = name_to_market.get(name, "unknown")

        if off_product:
            barcode = off_product.get("code") or off_product.get("_id") or ""
            score   = off_product.get("_match_score", 0)

            if barcode:
                _upsert_catalog(sb, off_product, barcode)
                _upsert_name_map(sb, name, market, barcode, score, "off_api")
                new_mappings[name] = barcode
                logger.debug(f"  [{i}/{len(unmapped)}] '{name}' → {barcode} (score={score:.0f})")
            else:
                _upsert_name_map(sb, name, market, None, score, "off_api_no_barcode")
                no_match_count += 1
        else:
            # Record that we tried — avoids re-querying next run
            _upsert_name_map(sb, name, market, None, 0, "off_api_no_match")
            no_match_count += 1

        # Polite delay between OFF API calls
        if i < len(unmapped):
            time.sleep(_OFF_DELAY)

    matched = len(existing) + len(new_mappings)
    logger.info(
        f"Enrichment complete: {matched} matched, {no_match_count} no-match "
        f"({len(new_mappings)} new via OFF API)"
    )

    return {**existing, **new_mappings}
