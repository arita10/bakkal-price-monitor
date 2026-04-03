"""
main.py — Bakkal Price Monitoring Orchestrator

Daily workflow:
  1. Load config from environment variables
  2. Fetch marketfiyati API → structured ProductData directly (no AI)
  3. Scrape cimri.com via Crawl4AI → parse with OpenAI GPT-4o Mini
  4. Scrape a101.com.tr via Crawl4AI → parse with OpenAI GPT-4o Mini
  5. Scrape bizimtoptan.com.tr via Playwright (no AI)
  6. Scrape carrefoursa.com via Playwright (no AI)
  7. Scrape migros.com.tr via Playwright (no AI)
  8. Scrape sokmarket.com.tr via Playwright (no AI)
  9. Scrape a101.com.tr/kapida via Playwright (no AI)
 10. Scrape BIM weekly flyers via Playwright + GPT-4o Vision (image OCR)
 11. For each product: compare with last Aiven price
      → Send Telegram BUY alert if price dropped >= threshold
      → Upsert current price into Aiven PostgreSQL
 12. Send daily summary to Telegram

Run locally:   python main.py
Run in CI:     triggered by .github/workflows/daily_price_check.yml
"""

import asyncio
import logging
import os

from src.alerts import send_daily_summary, send_price_drop_alert
from src.agents.bim_flyer_scraper import scrape_bim_flyers
from src.agents.crawl4ai_scraper import scrape_cimri, scrape_a101
from src.agents.marketfiyati_api import fetch_all as fetch_all_marketfiyati
from src.agents.parser import ProductData, build_client, parse_chunk
from src.config import load_config
from src.parsers.scrapers import (
    scrape_bizimtoptan,
    scrape_carrefoursa,
    scrape_migros,
    scrape_sok,
    scrape_a101kapida,
    scrape_essenjet,
)
from src.pipeline import get_last_prices, upsert_prices, init_supabase  # bulk ops

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bakkal_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("bakkal_monitor")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def run() -> None:
    """Full price monitoring run. Called once per GitHub Actions cron invocation."""
    logger.info("=== Bakkal Price Monitor starting ===")

    # ── 1. Configuration ─────────────────────────────────────────────────────
    config = load_config()
    threshold = config["PRICE_DROP_THRESHOLD"]

    # ── 2. Initialise clients ────────────────────────────────────────────────
    db_url = config["SUPABASE_URL"]
    sb_client = init_supabase(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    openai_client = build_client(config["OPENAI_API_KEY"])

    # ── Deduplication set ────────────────────────────────────────────────────
    seen_urls: set[str] = set()
    unique_products: list[ProductData] = []

    def _add_direct(dicts: list[dict]) -> int:
        """Add pre-structured product dicts (no AI) to unique_products."""
        count = 0
        for d in dicts:
            if d["product_url"] not in seen_urls and d["current_price"] > 0:
                seen_urls.add(d["product_url"])
                unique_products.append(ProductData(
                    product_name=d["product_name"],
                    current_price=d["current_price"],
                    market_name=d["market_name"],
                    product_url=d["product_url"],
                ))
                count += 1
        return count

    # ── 3-10. All scrapers in parallel ───────────────────────────────────────
    # marketfiyati (API), cimri (Crawl4AI), a101 (Crawl4AI), and all 5
    # Playwright shops run at the same time.
    logger.info("Starting all scrapers in parallel...")
    (
        mf_dicts,
        cimri_items,
        a101_items,
        bizim_raw,
        carrefour_raw,
        migros_raw,
        sok_raw,
        a101kapida_raw,
        essenjet_raw,
        bim_raw,
    ) = await asyncio.gather(
        fetch_all_marketfiyati(config),
        scrape_cimri(config),
        scrape_a101(config),
        scrape_bizimtoptan(),
        scrape_carrefoursa(),
        scrape_migros(),
        scrape_sok(),
        scrape_a101kapida(),
        scrape_essenjet(),
        scrape_bim_flyers(config),
    )

    # marketfiyati
    mf_added = _add_direct(mf_dicts)
    logger.info(f"marketfiyati: {mf_added} unique product(s) added")

    # cimri → OpenAI parse
    cimri_added = 0
    for i, raw in enumerate(cimri_items, start=1):
        logger.debug(f"OpenAI parsing cimri chunk {i}/{len(cimri_items)}")
        for p in parse_chunk(raw, openai_client):
            if p.product_url not in seen_urls and p.current_price > 0:
                seen_urls.add(p.product_url)
                unique_products.append(p)
                cimri_added += 1
    logger.info(f"cimri: {cimri_added} unique product(s) added via OpenAI")

    # a101 → OpenAI parse
    a101_added = 0
    for i, raw in enumerate(a101_items, start=1):
        logger.debug(f"OpenAI parsing a101 chunk {i}/{len(a101_items)}")
        for p in parse_chunk(raw, openai_client):
            if p.product_url not in seen_urls and p.current_price > 0:
                seen_urls.add(p.product_url)
                unique_products.append(p)
                a101_added += 1
    logger.info(f"a101: {a101_added} unique product(s) added via OpenAI")

    # Playwright shops
    bizim_added      = _add_direct(bizim_raw)
    carrefour_added  = _add_direct(carrefour_raw)
    migros_added     = _add_direct(migros_raw)
    sok_added        = _add_direct(sok_raw)
    a101kapida_added = _add_direct(a101kapida_raw)
    essenjet_added   = _add_direct(essenjet_raw)
    logger.info(
        f"Playwright done — "
        f"BizimToptan:{bizim_added} CarrefourSA:{carrefour_added} "
        f"Migros:{migros_added} SOK:{sok_added} A101Kapida:{a101kapida_added} "
        f"Essenjet:{essenjet_added}"
    )

    # BIM flyers (Playwright + GPT-4o Vision)
    bim_added = _add_direct(bim_raw)
    logger.info(f"BIM flyers: {bim_added} unique product(s) added via Vision")

    logger.info(f"Total unique products: {len(unique_products)}")

    # ── 11. Compare, alert, upsert ───────────────────────────────────────────
    total_alerts = 0

    # Filter out invalid products once
    valid_products = [
        p for p in unique_products
        if p.product_url and p.current_price > 0
    ]
    invalid_count = len(unique_products) - len(valid_products)
    if invalid_count:
        logger.debug(f"Skipping {invalid_count} invalid product(s)")

    # Bulk-fetch all previous prices in one query instead of N queries
    all_urls = [p.product_url for p in valid_products]
    last_prices = get_last_prices(db_url, all_urls)
    logger.info(f"Fetched last prices for {len(last_prices)} known product(s)")

    # Send price-drop alerts (still per-product — Telegram rate limit)
    for product in valid_products:
        last_price = last_prices.get(product.product_url)
        if last_price is not None and product.current_price < last_price:
            drop_pct = ((last_price - product.current_price) / last_price) * 100
            if drop_pct >= threshold:
                logger.info(
                    f"BUY alert: {product.product_name!r} "
                    f"{last_price:.2f} -> {product.current_price:.2f} TL "
                    f"({drop_pct:.1f}% drop)"
                )
                sent = send_price_drop_alert(
                    config["TELEGRAM_BOT_TOKEN"],
                    config["TELEGRAM_CHAT_ID"],
                    product,
                    last_price,
                    drop_pct,
                )
                if sent:
                    total_alerts += 1
                await asyncio.sleep(0.5)

    # Bulk-upsert all products in one batched call
    total_scraped, total_errors = upsert_prices(db_url, valid_products, last_prices)
    total_errors += invalid_count

    # ── 12. Daily summary ────────────────────────────────────────────────────
    send_daily_summary(
        config["TELEGRAM_BOT_TOKEN"],
        config["TELEGRAM_CHAT_ID"],
        total_scraped,
        total_alerts,
        total_errors,
    )

    logger.info(
        f"=== Run complete — scraped: {total_scraped}, "
        f"alerts: {total_alerts}, errors: {total_errors} ==="
    )


if __name__ == "__main__":
    asyncio.run(run())
