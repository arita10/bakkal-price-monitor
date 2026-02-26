"""
main.py — Bakkal Price Monitoring Orchestrator

Daily workflow:
  1. Load config from environment variables
  2. Fetch marketfiyati API → structured ProductData directly (no AI)
  3. Scrape cimri.com via Crawl4AI → parse with OpenAI GPT-4o Mini
  4. Scrape essenjet.com + bizimtoptan.com.tr via Playwright (no AI)
  5. For each product: compare with last Supabase price
       → Send Telegram BUY alert if price dropped >= threshold
       → Upsert current price into Supabase
  6. Send daily summary to Telegram

Run locally:   python main.py
Run in CI:     triggered by .github/workflows/daily_price_check.yml
"""

import asyncio
import logging

from supabase import create_client

from alerts import send_daily_summary, send_price_drop_alert
from config import load_config
from parser import ProductData, build_gemini_client, parse_chunk
from scraper import fetch_all_marketfiyati, scrape_cimri, scrape_essen_direct, scrape_bizimtoptan_direct
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
    openai_client = build_gemini_client(config["OPENAI_API_KEY"])

    # ── 3. marketfiyati API → direct structured dicts (no OpenAI needed) ────
    seen_urls: set[str] = set()
    unique_products: list[ProductData] = []

    mf_dicts = await fetch_all_marketfiyati(config)
    mf_added = 0
    for d in mf_dicts:
        if d["product_url"] not in seen_urls and d["current_price"] > 0:
            seen_urls.add(d["product_url"])
            unique_products.append(ProductData(
                product_name=d["product_name"],
                current_price=d["current_price"],
                market_name=d["market_name"],
                product_url=d["product_url"],
            ))
            mf_added += 1
    logger.info(f"marketfiyati: {mf_added} unique product(s) added (no OpenAI)")

    # ── 4. cimri.com via Crawl4AI → parse with OpenAI (HTML needs AI) ───────
    cimri_items = await scrape_cimri(config)
    logger.info(f"cimri: {len(cimri_items)} chunk(s) to parse with OpenAI")

    cimri_added = 0
    for i, raw in enumerate(cimri_items, start=1):
        logger.debug(f"OpenAI parsing cimri chunk {i}/{len(cimri_items)}")
        products = parse_chunk(raw, openai_client)
        for p in products:
            if p.product_url not in seen_urls and p.current_price > 0:
                seen_urls.add(p.product_url)
                unique_products.append(p)
                cimri_added += 1
        await asyncio.sleep(0.5)
    logger.info(f"cimri: {cimri_added} unique product(s) added via OpenAI")

    # ── 5. essenjet.com directly (Playwright, no AI) ─────────────────────────
    essen_dicts = await scrape_essen_direct()
    essen_added = 0
    for d in essen_dicts:
        if d["product_url"] not in seen_urls and d["current_price"] > 0:
            seen_urls.add(d["product_url"])
            unique_products.append(ProductData(
                product_name=d["product_name"],
                current_price=d["current_price"],
                market_name=d["market_name"],
                product_url=d["product_url"],
            ))
            essen_added += 1
    logger.info(f"Essen JET: {essen_added} unique product(s) added (no OpenAI)")

    # ── 6. bizimtoptan.com.tr directly (Playwright, no AI) ───────────────────
    bizim_dicts = await scrape_bizimtoptan_direct()
    bizim_added = 0
    for d in bizim_dicts:
        if d["product_url"] not in seen_urls and d["current_price"] > 0:
            seen_urls.add(d["product_url"])
            unique_products.append(ProductData(
                product_name=d["product_name"],
                current_price=d["current_price"],
                market_name=d["market_name"],
                product_url=d["product_url"],
            ))
            bizim_added += 1
    logger.info(f"Bizim Toptan: {bizim_added} unique product(s) added (no OpenAI)")

    logger.info(f"Total unique products: {len(unique_products)}")

    # ── 7. Compare, alert, upsert ────────────────────────────────────────────
    total_scraped = 0
    total_alerts = 0
    total_errors = 0

    for product in unique_products:
        if not product.product_url or product.current_price <= 0:
            logger.debug(f"Skipping invalid product: {product.product_name!r}")
            total_errors += 1
            continue

        total_scraped += 1

        last_price = get_last_price(supabase, product.product_url)

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
                await asyncio.sleep(0.5)

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
