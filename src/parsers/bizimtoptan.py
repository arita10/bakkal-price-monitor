"""
src/parsers/bizimtoptan.py — BizimToptan scraper (bizimtoptan.com.tr)

Confirmed selectors:
  card:   .product-box-container
  name:   .productbox-name
  price:  .campaign-price  (fallback: .product-price)
  link:   a.product-item

Products rendered via jQuery tmpl — waits until template placeholders resolve.
No pagination — all products shown per campaign/discount page.
"""

import asyncio
import logging

from playwright.async_api import async_playwright

from src.browsers.playwright_browser import new_context
from src.utils import parse_tr_price

logger = logging.getLogger("bakkal_monitor.parsers.bizimtoptan")

TARGET_URLS = [
    "https://www.bizimtoptan.com.tr/kampanyalar",
    "https://www.bizimtoptan.com.tr/indirimli-urunler",
]


async def scrape() -> list[dict]:
    """
    Scrape bizimtoptan.com.tr campaign/discount pages using Playwright.
    Returns list of dicts: product_name, current_price, market_name, product_url.
    """
    products = []

    async with async_playwright() as p:
        browser, context = await new_context(p)

        for url in TARGET_URLS:
            try:
                logger.info(f"BizimToptan: scraping {url}")
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=60_000)

                # Wait for jQuery tmpl to render (placeholder "${item.label}" must resolve)
                try:
                    await page.wait_for_function(
                        """() => {
                            const els = document.querySelectorAll('.productbox-name');
                            return Array.from(els).some(
                                el => el.innerText && !el.innerText.includes('${')
                            );
                        }""",
                        timeout=15_000,
                    )
                except Exception:
                    logger.warning(f"BizimToptan: tmpl render timeout at {url}, trying anyway")
                await asyncio.sleep(2)

                cards = await page.query_selector_all(".product-box-container")
                if not cards:
                    logger.warning(f"BizimToptan: no product cards at {url}")
                    await page.close()
                    continue

                page_count = 0
                for card in cards:
                    try:
                        name_el  = await card.query_selector(".productbox-name")
                        price_el = await card.query_selector(".campaign-price")
                        if not price_el:
                            price_el = await card.query_selector(".product-price")
                        link_el  = await card.query_selector("a.product-item")

                        if not name_el or not price_el:
                            continue

                        name = (await name_el.inner_text()).strip()
                        price_raw = (await price_el.inner_text()).strip()

                        if "${" in name or "${" in price_raw:
                            continue

                        price = parse_tr_price(price_raw)
                        if not name or price <= 0:
                            continue

                        href = await link_el.get_attribute("href") if link_el else ""
                        if href and ("${" in href or href.startswith("javascript")):
                            href = ""
                        if href and not href.startswith("http"):
                            product_url = f"https://www.bizimtoptan.com.tr/{href.lstrip('/')}"
                        else:
                            product_url = href or url

                        products.append({
                            "product_name": name,
                            "current_price": price,
                            "market_name": "Bizim Toptan",
                            "product_url": product_url,
                        })
                        page_count += 1

                    except Exception as card_exc:
                        logger.debug(f"BizimToptan card error: {card_exc}")

                logger.info(f"BizimToptan: {page_count} products from {url}")
                await page.close()
                await asyncio.sleep(2)

            except Exception as exc:
                logger.error(f"BizimToptan page error for {url}: {repr(exc)}")

        await browser.close()

    logger.info(f"BizimToptan scrape complete: {len(products)} total products")
    return products
