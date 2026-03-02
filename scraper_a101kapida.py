"""
scraper_a101kapida.py — A101 Kapıda scraper (a101.com.tr/kapida)

Confirmed selectors (inspect_a101.py):
  card:   [data-product-id]               (all products rendered on one page)
  name:   a[href*='kapida'] img[alt]       second <img> inside card has real product name
  price:  last ₺X,XX value in card text   e.g. card text ends with '₺9,50' (unit price)
  link:   a[href*='kapida']               relative href e.g. /kapida/su-icecek/..._p-13000781
Cookie dialog: dismissed via button:has-text('Kabul Et')
Pagination: all products loaded at once (no pagination detected).
"""

import asyncio
import logging
import re

from playwright.async_api import async_playwright

logger = logging.getLogger("bakkal_monitor.scraper_a101kapida")

BASE_URL = "https://www.a101.com.tr"

A101_KAPIDA_TARGET_URLS = [
    "https://www.a101.com.tr/kapida/doritos-urunleri-S4289",
    "https://www.a101.com.tr/kapida/bizim-yag-S1983",
    "https://www.a101.com.tr/kapida/haftanin-yildizlari",
    "https://www.a101.com.tr/kapida/10tl-ve-uzeri-alisverislerinizde-indirimli-urunler",
    "https://www.a101.com.tr/kapida/cok-al-az-ode",
    "https://www.a101.com.tr/kapida/aldin-aldin",
]


def _parse_kapida_price(card_text: str) -> float:
    """
    Extract unit price from A101 Kapida card text.
    Card text format: '6 AL\\n₺57,\\n00\\n₺49,\\n50\\nProduct Name\\n₺9,50'
    The unit price is the last ₺X,XX value.
    """
    # Remove newlines to make pattern matching easier
    text = card_text.replace("\n", " ")
    # Find all price-like patterns: ₺ followed by digits and comma
    matches = re.findall(r"₺\s*(\d[\d.]*,\d+)", text)
    if matches:
        raw = matches[-1].replace(".", "").replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.0


async def _dismiss_cookie(page) -> None:
    """Click the cookie consent button if present."""
    for btn_sel in [
        "button:has-text('Kabul Et')",
        "#CybotCookiebotDialogBodyButtonAccept",
        "#CybotCookiebotDialogBodyLevelButtonAccept",
        "button:has-text('Tümünü Kabul')",
        "button:has-text('Accept')",
    ]:
        try:
            btn = await page.query_selector(btn_sel)
            if btn:
                await btn.click()
                await asyncio.sleep(2)
                return
        except Exception:
            pass


async def scrape_a101kapida_direct() -> list:
    """
    Scrape all A101_KAPIDA_TARGET_URLS using Playwright.
    All products load on a single page (no pagination).
    Returns list of dicts: product_name, current_price, market_name, product_url.
    """
    products = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
        )
        cookie_dismissed = False

        for base_url in A101_KAPIDA_TARGET_URLS:
            try:
                logger.info(f"A101 Kapida: scraping {base_url}")
                page = await context.new_page()
                await page.goto(base_url, wait_until="networkidle", timeout=60_000)
                await asyncio.sleep(3)

                # Dismiss cookie dialog once per session
                if not cookie_dismissed:
                    await _dismiss_cookie(page)
                    cookie_dismissed = True
                    await asyncio.sleep(2)

                # Wait for product cards to render
                try:
                    await page.wait_for_function(
                        "() => document.querySelectorAll('[data-product-id]').length > 0",
                        timeout=15_000,
                    )
                except Exception:
                    logger.warning(f"A101 Kapida: render timeout for {base_url}, trying anyway")

                await asyncio.sleep(2)

                cards = await page.query_selector_all("[data-product-id]")
                if not cards:
                    logger.warning(f"A101 Kapida: no cards found at {base_url}")
                    await page.close()
                    continue

                page_count = 0
                for card in cards:
                    try:
                        # Name: second img inside the product link has the real alt
                        link_el = await card.query_selector("a[href*='kapida']")
                        name = ""
                        href = ""
                        if link_el:
                            href = await link_el.get_attribute("href") or ""
                            imgs = await link_el.query_selector_all("img[alt]")
                            for img in imgs:
                                alt = (await img.get_attribute("alt") or "").strip()
                                # Skip generic banner alts (match product name pattern)
                                if alt and not any(
                                    slug in alt.lower()
                                    for slug in ["cok-al", "haftanin", "indirimli", "aldin", "bizim", "doritos"]
                                ):
                                    name = alt
                                    break

                        if not name:
                            # Fallback: extract name from card text (line before last price)
                            card_text = (await card.inner_text()).strip()
                            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
                            # Find lines that look like product names (not prices/promos)
                            for line in reversed(lines):
                                if not re.match(r"^[₺\d\s,AL]+$", line) and len(line) > 3:
                                    name = line
                                    break

                        card_text = (await card.inner_text()).strip()
                        price = _parse_kapida_price(card_text)

                        if not name or price <= 0:
                            continue

                        if href and not href.startswith("http"):
                            product_url = BASE_URL + "/" + href.lstrip("/")
                        else:
                            product_url = href or base_url

                        products.append({
                            "product_name": name,
                            "current_price": price,
                            "market_name": "A101 Kapida",
                            "product_url": product_url,
                        })
                        page_count += 1

                    except Exception as card_exc:
                        logger.debug(f"A101 Kapida card error: {card_exc}")

                logger.info(f"A101 Kapida: {page_count} products from {base_url}")
                await page.close()
                await asyncio.sleep(1)

            except Exception as exc:
                logger.error(f"A101 Kapida page error for {base_url}: {repr(exc)}")

        await browser.close()

    logger.info(f"A101 Kapida scrape complete: {len(products)} total products")
    return products
