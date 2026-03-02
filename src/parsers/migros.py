"""
src/parsers/migros.py — Migros scraper (migros.com.tr)

Confirmed selectors (inspect_migros.py):
  card:   sm-list-page-item          (Angular component, 30 per page)
  name:   [class*='product-name']
  price:  [class*='sale-price'] -> fallback [class*='price']
            note: price text may be "Iyi Fiyat\n11,95 TL" — cleaned automatically
  link:   a[class*='product']       relative href e.g. /patates-kg-p-1afabf4

Pagination: ?sayfa=N  (page 1 = no param, page 2 = ?sayfa=2, ...)
Max 20 pages per category (safety cap).
"""

import asyncio
import logging

from playwright.async_api import async_playwright

from src.browsers.playwright_browser import new_context
from src.utils import parse_tr_price

logger = logging.getLogger("bakkal_monitor.parsers.migros")

BASE_URL = "https://www.migros.com.tr"
MAX_PAGES = 20

TARGET_URLS = [
    "https://www.migros.com.tr/tum-indirimli-urunler-dt-0",
    "https://www.migros.com.tr/beslenme-yasam-tarzi-ptt-1",
    "https://www.migros.com.tr/sadece-migrosta-ptt-2",
    "https://www.migros.com.tr/migroskop-urunleri-dt-3",
    "https://www.migros.com.tr/ramazan-c-1209a",
    "https://www.migros.com.tr/meyve-sebze-c-2",
    "https://www.migros.com.tr/et-tavuk-balik-c-3",
    "https://www.migros.com.tr/sut-kahvaltilik-c-4",
    "https://www.migros.com.tr/temel-gida-c-5",
    "https://www.migros.com.tr/icecek-c-6",
    "https://www.migros.com.tr/reis-c-1222a",
    "https://www.migros.com.tr/atistirmalik-c-113fb",
    "https://www.migros.com.tr/dondurma-c-41b",
    "https://www.migros.com.tr/firin-pastane-c-7e",
    "https://www.migros.com.tr/bizim-yag-c-12155",
    "https://www.migros.com.tr/hazir-yemek-donuk-c-7d",
    "https://www.migros.com.tr/gurmepack-yemek-c-121f5",
    "https://www.migros.com.tr/deterjan-temizlik-c-7",
    "https://www.migros.com.tr/kisisel-bakim-kozmetik-saglik-c-8",
    "https://www.migros.com.tr/kagit-islak-mendil-c-8d",
    "https://www.migros.com.tr/bebek-c-9",
    "https://www.migros.com.tr/ev-yasam-c-a",
    "https://www.migros.com.tr/kitap-kirtasiye-oyuncak-c-118ec",
    "https://www.migros.com.tr/evcil-hayvan-c-a0",
]


def _page_url(base: str, page_num: int) -> str:
    if page_num <= 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}sayfa={page_num}"


async def scrape() -> list[dict]:
    """
    Scrape all Migros TARGET_URLS using Playwright.
    Paginates via ?sayfa=N until a page returns 0 cards or MAX_PAGES reached.
    Returns list of dicts: product_name, current_price, market_name, product_url.
    """
    products = []

    async with async_playwright() as p:
        browser, context = await new_context(p)

        for base_url in TARGET_URLS:
            url_total = 0
            try:
                logger.info(f"Migros: scraping {base_url}")
                page = await context.new_page()

                for page_num in range(1, MAX_PAGES + 1):
                    url = _page_url(base_url, page_num)
                    await page.goto(url, wait_until="networkidle", timeout=60_000)

                    try:
                        await page.wait_for_function(
                            "() => document.querySelectorAll('sm-list-page-item').length > 0",
                            timeout=15_000,
                        )
                    except Exception:
                        logger.warning(f"Migros: render timeout on page {page_num} of {base_url}")

                    await asyncio.sleep(2)

                    cards = await page.query_selector_all("sm-list-page-item")
                    if not cards:
                        logger.info(f"Migros: no cards on page {page_num} — stopping pagination")
                        break

                    page_count = 0
                    for card in cards:
                        try:
                            name_el  = await card.query_selector("[class*='product-name']")
                            price_el = await card.query_selector("[class*='sale-price']")
                            if not price_el:
                                price_el = await card.query_selector("[class*='price']")
                            link_el  = await card.query_selector("a[class*='product']")

                            if not name_el or not price_el:
                                continue

                            name = (await name_el.inner_text()).strip()
                            price_raw = (await price_el.inner_text()).strip()
                            price = parse_tr_price(price_raw)

                            if not name or price <= 0:
                                continue

                            href = await link_el.get_attribute("href") if link_el else ""
                            if href and not href.startswith("http"):
                                product_url = BASE_URL + "/" + href.lstrip("/")
                            else:
                                product_url = href or base_url

                            products.append({
                                "product_name": name,
                                "current_price": price,
                                "market_name": "Migros",
                                "product_url": product_url,
                            })
                            page_count += 1

                        except Exception as card_exc:
                            logger.debug(f"Migros card error: {card_exc}")

                    url_total += page_count
                    logger.info(f"Migros: page {page_num} of {base_url} -> {page_count} products")

                    if len(cards) < 30:
                        break

                    if page_num == MAX_PAGES:
                        logger.warning(f"Migros: hit {MAX_PAGES}-page cap for {base_url}")

                logger.info(f"Migros: {url_total} products total from {base_url}")
                await page.close()
                await asyncio.sleep(1)

            except Exception as exc:
                logger.error(f"Migros page error for {base_url}: {repr(exc)}")

        await browser.close()

    logger.info(f"Migros scrape complete: {len(products)} total products")
    return products
