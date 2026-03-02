"""
src/parsers/sok.py — SOK Market scraper (sokmarket.com.tr)

Confirmed selectors (inspect_sok.py):
  card:   [class*='ProductCard']    (up to 122 products loaded at once)
  name:   h2
  price:  span[class*='price']      e.g. '17,90₺'
  link:   closest <a> ancestor      e.g. /patates-kg-p-35919

Pagination: ?page=N  (stops when a page returns 0 cards or same first product).
Max 10 pages per category (safety cap).
"""

import asyncio
import logging

from playwright.async_api import async_playwright

from src.browsers.playwright_browser import new_context
from src.utils import parse_tr_price

logger = logging.getLogger("bakkal_monitor.parsers.sok")

BASE_URL = "https://www.sokmarket.com.tr"
MAX_PAGES = 10

TARGET_URLS = [
    "https://www.sokmarket.com.tr/win-kazandiran-urunler-pgrp-f353cf31-f728-425e-a453-5774219a76b8",
    "https://www.sokmarket.com.tr/haftanin-firsatlari-market-sgrp-146401",
    "https://www.sokmarket.com.tr/50-tl-ve-uzeri-indirimli-urunler-pgrp-11d42a6b-df28-4fe6-b1a3-7ad6b8d7f9a0",
    "https://www.sokmarket.com.tr/glutensiz-urunler-sgrp-172676",
    "https://www.sokmarket.com.tr/yemeklik-malzemeler-c-1770",
    "https://www.sokmarket.com.tr/et-ve-tavuk-ve-sarkuteri-c-160",
    "https://www.sokmarket.com.tr/meyve-ve-sebze-c-20",
    "https://www.sokmarket.com.tr/sut-ve-sut-urunleri-c-460",
    "https://www.sokmarket.com.tr/kahvaltilik-c-890",
    "https://www.sokmarket.com.tr/atistirmaliklar-c-20376",
    "https://www.sokmarket.com.tr/icecek-c-20505",
    "https://www.sokmarket.com.tr/ekmek-ve-pastane-c-1250",
    "https://www.sokmarket.com.tr/dondurulmus-urunler-c-1550",
    "https://www.sokmarket.com.tr/dondurma-c-31102",
    "https://www.sokmarket.com.tr/temizlik-c-20647",
    "https://www.sokmarket.com.tr/kagit-urunler-c-20875",
    "https://www.sokmarket.com.tr/kisisel-bakim-ve-kozmetik-c-20395",
    "https://www.sokmarket.com.tr/anne-bebek-ve-cocuk-c-20634",
    "https://www.sokmarket.com.tr/evcil-dostlar-c-20880",
]


def _page_url(base: str, page_num: int) -> str:
    if page_num <= 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}page={page_num}"


async def scrape() -> list[dict]:
    """
    Scrape all SOK TARGET_URLS using Playwright.
    Paginates via ?page=N, stops when 0 cards or first product repeats.
    Returns list of dicts: product_name, current_price, market_name, product_url.
    """
    products = []

    async with async_playwright() as p:
        browser, context = await new_context(p)

        for base_url in TARGET_URLS:
            url_total = 0
            first_product_page1 = None
            try:
                logger.info(f"SOK: scraping {base_url}")
                page = await context.new_page()

                for page_num in range(1, MAX_PAGES + 1):
                    url = _page_url(base_url, page_num)
                    await page.goto(url, wait_until="networkidle", timeout=60_000)
                    await asyncio.sleep(2)

                    cards = await page.query_selector_all("[class*='ProductCard']")
                    if not cards:
                        logger.info(f"SOK: no cards on page {page_num} of {base_url} — stopping")
                        break

                    page_count = 0
                    for card in cards:
                        try:
                            name_el  = await card.query_selector("h2")
                            price_el = await card.query_selector("span[class*='price']")

                            if not name_el or not price_el:
                                continue

                            name = (await name_el.inner_text()).strip()
                            price_raw = (await price_el.inner_text()).strip()
                            price = parse_tr_price(price_raw)

                            if not name or price <= 0:
                                continue

                            href = await card.evaluate(
                                "el => el.closest('a') ? el.closest('a').getAttribute('href') : ''"
                            )
                            if href and not href.startswith("http"):
                                product_url = BASE_URL + "/" + href.lstrip("/")
                            else:
                                product_url = href or base_url

                            if page_num == 1 and page_count == 0:
                                first_product_page1 = name
                            elif page_num > 1 and page_count == 0:
                                if name == first_product_page1:
                                    logger.info(
                                        f"SOK: page {page_num} repeats page 1 — stopping pagination"
                                    )
                                    cards = []
                                    break

                            products.append({
                                "product_name": name,
                                "current_price": price,
                                "market_name": "SOK Market",
                                "product_url": product_url,
                            })
                            page_count += 1

                        except Exception as card_exc:
                            logger.debug(f"SOK card error: {card_exc}")

                    if not cards:
                        break

                    url_total += page_count
                    logger.info(f"SOK: page {page_num} of {base_url} -> {page_count} products")

                    if page_num == MAX_PAGES:
                        logger.warning(f"SOK: hit {MAX_PAGES}-page cap for {base_url}")

                logger.info(f"SOK: {url_total} products total from {base_url}")
                await page.close()
                await asyncio.sleep(1)

            except Exception as exc:
                logger.error(f"SOK page error for {base_url}: {repr(exc)}")

        await browser.close()

    logger.info(f"SOK scrape complete: {len(products)} total products")
    return products
