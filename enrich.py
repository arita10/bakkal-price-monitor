"""
enrich.py — One-time / on-demand product enrichment script.

Reads all distinct product names from sp_products, looks up barcodes
via Open Food Facts API, and saves results to:
  - sp_product_catalog   (barcode, canonical_name, brand, …)
  - sp_product_name_map  (scraped_name → barcode mapping + score)

Already-mapped names are skipped (cache-first). Safe to re-run anytime —
only NEW names since the last run will query the OFF API.

Usage:
  python enrich.py
"""

import logging
import os

from dotenv import load_dotenv
from supabase import create_client

from src.enrichment import enrich_products
from src.agents.parser import ProductData

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("enrich")


def main():
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    # Fetch all distinct product names from sp_products
    logger.info("Fetching all product names from sp_products...")
    resp = sb.table("sp_products").select("product_name, market_name, product_url").execute()
    rows = resp.data or []
    logger.info(f"Found {len(rows)} products in sp_products")

    # Convert to ProductData list (only name/market needed for enrichment)
    products = [
        ProductData(
            product_name=r["product_name"],
            market_name=r["market_name"],
            current_price=0,
            product_url=r["product_url"],
        )
        for r in rows
        if r.get("product_name")
    ]

    # Run enrichment — skips already-mapped names automatically
    matched = enrich_products(sb, products)
    logger.info(f"Done. {len(matched)} total barcodes mapped.")


if __name__ == "__main__":
    main()
