"""
main.py — Bakkal Price Monitoring Orchestrator

Daily workflow:
  1. Load config from environment variables
  2. Fetch raw product data (marketfiyati API + cimri.com via Crawl4AI)
  3. Parse chunks with Gemini 1.5 Flash → structured ProductData
  4. For each product: compare with last Supabase price
       → Send Telegram BUY alert if price dropped >= threshold
       → Upsert current price into Supabase
  5. Send daily summary to Telegram

Run locally:   python main.py
Run in CI:     triggered by .github/workflows/daily_price_check.yml
"""

import asyncio
import logging

from supabase import create_client

from alerts import send_daily_summary, send_price_drop_alert
from config import load_config
from parser import ProductData, build_gemini_client, parse_chunk
from scraper import fetch_all_marketfiyati, scrape_cimri
from storage import get_last_price, upsert_price

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bakkal_monitor")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def run() -> None:
    """
    Full price monitoring run. Called once per GitHub Actions cron invocation.
    """
    logger.info("=== Bakkal Price Monitor starting ===")

    # ── 1. Configuration ────────────────────────────────────────────────────
    config = load_config()
    threshold = config["PRICE_DROP_THRESHOLD"]

    # ── 2. Initialise clients ────────────────────────────────────────────────
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    gemini_model = build_gemini_client(config["OPENAI_API_KEY"])

    # ── 3. Scrape — marketfiyati API ─────────────────────────────────────────
    raw_items = await fetch_all_marketfiyati(config)

    # ── 4. Scrape — cimri.com via Crawl4AI ──────────────────────────────────
    cimri_items = await scrape_cimri(config)
    raw_items.extend(cimri_items)

    logger.info(f"Total raw chunks to parse with Gemini: {len(raw_items)}")

    # ── 5. Parse chunks with Gemini ──────────────────────────────────────────
    all_products: list[ProductData] = []
    for i, raw in enumerate(raw_items, start=1):
        logger.debug(f"Parsing chunk {i}/{len(raw_items)} [{raw.source}]")
        products = parse_chunk(raw, gemini_model)
        all_products.extend(products)
        # Gemini 1.5 Flash free tier: 15 RPM → 4-second gap keeps us safe
        await asyncio.sleep(4.0)

    logger.info(f"Gemini extracted {len(all_products)} product(s) total")

    # ── 6. Deduplicate by product_url ────────────────────────────────────────
    seen_urls: set[str] = set()
    unique_products: list[ProductData] = []
    for product in all_products:
        if product.product_url not in seen_urls:
            seen_urls.add(product.product_url)
            unique_products.append(product)

    logger.info(
        f"After deduplication: {len(unique_products)} unique product(s)"
    )

    # ── 7. Compare, alert, upsert ────────────────────────────────────────────
    total_scraped = 0
    total_alerts = 0
    total_errors = 0

    for product in unique_products:
        # Basic sanity check
        if not product.product_url or product.current_price <= 0:
            logger.debug(f"Skipping invalid product: {product.product_name!r}")
            total_errors += 1
            continue

        total_scraped += 1

        # Fetch last known price
        last_price = get_last_price(supabase, product.product_url)

        # Check for a price drop that meets the threshold
        if last_price is not None and product.current_price < last_price:
            drop_pct = ((last_price - product.current_price) / last_price) * 100
            if drop_pct >= threshold:
                logger.info(
                    f"BUY alert: {product.product_name!r} "
                    f"{last_price:.2f} → {product.current_price:.2f} TL "
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
                # Pause briefly to avoid Telegram 429 rate-limit
                await asyncio.sleep(0.5)

        # Always persist the current price
        success = upsert_price(supabase, product, last_price)
        if not success:
            total_errors += 1

    # ── 8. Daily summary ─────────────────────────────────────────────────────
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
