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
 10. For each product: compare with last Supabase price
      → Send Telegram BUY alert if price dropped >= threshold
      → Upsert current price into Supabase
 11. Send daily summary to Telegram

Run locally:   python main.py
Run in CI:     triggered by .github/workflows/daily_price_check.yml
"""

import asyncio
import logging
import os

from supabase import create_client

from src.alerts import send_daily_summary, send_price_drop_alert
from src.agents.crawl4ai_scraper import scrape_cimri, scrape_a101
from src.agents.marketfiyati_api import fetch_all as fetch_all_marketfiyati
from src.agents.parser import ProductData, build_client, parse_chunk
from src.config import load_config
from src.parsers.bizimtoptan import scrape as scrape_bizimtoptan
from src.parsers.carrefoursa import scrape as scrape_carrefoursa
from src.parsers.migros import scrape as scrape_migros
from src.parsers.sok import scrape as scrape_sok
from src.parsers.a101kapida import scrape as scrape_a101kapida
from src.pipeline import get_last_price, upsert_price

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
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
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

    # ── 3. marketfiyati API (no AI) ──────────────────────────────────────────
    mf_dicts = await fetch_all_marketfiyati(config)
    mf_added = _add_direct(mf_dicts)
    logger.info(f"marketfiyati: {mf_added} unique product(s) added (no OpenAI)")

    # ── 4. cimri.com via Crawl4AI + OpenAI ───────────────────────────────────
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

    # ── 5. a101.com.tr via Crawl4AI + OpenAI ────────────────────────────────
    a101_items = await scrape_a101(config)
    logger.info(f"a101: {len(a101_items)} chunk(s) to parse with OpenAI")
    a101_added = 0
    for i, raw in enumerate(a101_items, start=1):
        logger.debug(f"OpenAI parsing a101 chunk {i}/{len(a101_items)}")
        products = parse_chunk(raw, openai_client)
        for p in products:
            if p.product_url not in seen_urls and p.current_price > 0:
                seen_urls.add(p.product_url)
                unique_products.append(p)
                a101_added += 1
        await asyncio.sleep(0.5)
    logger.info(f"a101: {a101_added} unique product(s) added via OpenAI")

    # ── 6. BizimToptan (Playwright, no AI) ───────────────────────────────────
    bizim_added = _add_direct(await scrape_bizimtoptan())
    logger.info(f"Bizim Toptan: {bizim_added} unique product(s) added (no OpenAI)")

    # ── 7. CarrefourSA (Playwright, no AI) ───────────────────────────────────
    carrefour_added = _add_direct(await scrape_carrefoursa())
    logger.info(f"CarrefourSA: {carrefour_added} unique product(s) added (no OpenAI)")

    # ── 8. Migros (Playwright, no AI) ────────────────────────────────────────
    migros_added = _add_direct(await scrape_migros())
    logger.info(f"Migros: {migros_added} unique product(s) added (no OpenAI)")

    # ── 9. SOK Market (Playwright, no AI) ────────────────────────────────────
    sok_added = _add_direct(await scrape_sok())
    logger.info(f"SOK Market: {sok_added} unique product(s) added (no OpenAI)")

    # ── 10. A101 Kapida (Playwright, no AI) ──────────────────────────────────
    a101kapida_added = _add_direct(await scrape_a101kapida())
    logger.info(f"A101 Kapida: {a101kapida_added} unique product(s) added (no OpenAI)")

    logger.info(f"Total unique products: {len(unique_products)}")

    # ── 11. Compare, alert, upsert ───────────────────────────────────────────
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

        success = upsert_price(supabase, product, last_price)
        if not success:
            total_errors += 1

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
