"""
src/parsers/base.py — Common scrape engine shared by all shop scrapers.

Defines ShopConfig (per-shop selectors + pagination) and
scrape_shop() which runs the full Playwright scrape loop for any shop.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from src.utils import parse_tr_price

logger = logging.getLogger("bakkal_monitor.parsers.base")

# ── Pagination types ──────────────────────────────────────────────────────────

URL_PARAM   = "url_param"    # ?page=N or ?sayfa=N
NEXT_BUTTON = "next_button"  # click a next/pager button
NONE        = "none"         # all products on one page


# ── Shop configuration ────────────────────────────────────────────────────────

@dataclass
class ShopConfig:
    market_name:    str                        # e.g. "Migros"
    base_url:       str                        # scheme+host, e.g. "https://www.migros.com.tr"
    target_urls:    list[str]                  # category pages to scrape
    card_sel:       str                        # CSS selector for a product card
    name_sel:       str                        # inside card
    price_sel:      str                        # inside card
    link_sel:       str        = ""            # inside card; empty = use closest <a>
    link_via_parent: bool      = False         # True = use el.closest('a')
    fallback_price_sel: str    = ""            # tried when price_sel returns nothing
    pagination:     str        = NONE          # URL_PARAM | NEXT_BUTTON | NONE
    page_param:     str        = "page"        # URL param name (e.g. "sayfa", "page")
    next_btn_sel:   str        = ""            # selector for Next button
    max_pages:      int        = 20
    # Called after page.goto() — for custom waits (Angular, jQuery tmpl, etc.)
    pre_scrape_hook: Callable | None = field(default=None, repr=False)
    # Called once before first URL if cookie dialog expected
    cookie_sel:     str        = ""
    # Custom name extractor — overrides default name_sel logic
    extract_name:   Callable | None = field(default=None, repr=False)
    # Custom price extractor — overrides default price_sel logic
    extract_price:  Callable | None = field(default=None, repr=False)


# ── URL helpers ───────────────────────────────────────────────────────────────

def _page_url(base: str, param: str, page_num: int) -> str:
    if page_num <= 1:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{param}={page_num}"


def _absolute(href: str, base_url: str, fallback: str) -> str:
    if not href:
        return fallback
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


# ── Core engine ───────────────────────────────────────────────────────────────

async def scrape_shop(config: ShopConfig) -> list[dict]:
    """
    Run the full scrape loop for one shop using its ShopConfig.
    Returns list[dict] with keys: product_name, current_price, market_name, product_url.
    """
    from playwright.async_api import async_playwright
    from src.browsers.playwright_browser import new_context

    products: list[dict] = []
    name = config.market_name

    async with async_playwright() as p:
        browser, context = await new_context(p)
        cookie_dismissed = False

        for base_url in config.target_urls:
            url_total = 0
            try:
                logger.info(f"{name}: scraping {base_url}")
                page = await context.new_page()
                await page.goto(base_url, wait_until="networkidle", timeout=60_000)

                # ── Cookie dismiss (once per browser session) ─────────────────
                if config.cookie_sel and not cookie_dismissed:
                    try:
                        btn = await page.query_selector(config.cookie_sel)
                        if btn:
                            await btn.click()
                            await asyncio.sleep(2)
                            cookie_dismissed = True
                            logger.debug(f"{name}: cookie dismissed")
                    except Exception:
                        pass

                # ── Pre-scrape hook (custom wait / setup) ─────────────────────
                if config.pre_scrape_hook:
                    await config.pre_scrape_hook(page)

                # ── Pagination loop ───────────────────────────────────────────
                page_num = 1
                first_product_name = None   # used by url_param duplicate-check

                while page_num <= config.max_pages:
                    # Navigate to paginated URL (url_param mode)
                    if config.pagination == URL_PARAM and page_num > 1:
                        url = _page_url(base_url, config.page_param, page_num)
                        await page.goto(url, wait_until="networkidle", timeout=30_000)
                        if config.pre_scrape_hook:
                            await config.pre_scrape_hook(page)

                    cards = await page.query_selector_all(config.card_sel)
                    if not cards:
                        logger.info(f"{name}: no cards on page {page_num} — stopping")
                        break

                    page_count = 0
                    for card in cards:
                        try:
                            # ── Name ──────────────────────────────────────────
                            if config.extract_name:
                                product_name = await config.extract_name(card)
                            else:
                                el = await card.query_selector(config.name_sel)
                                product_name = (await el.inner_text()).strip() if el else ""

                            # ── Price ─────────────────────────────────────────
                            if config.extract_price:
                                price = await config.extract_price(card)
                            else:
                                el = await card.query_selector(config.price_sel)
                                if not el and config.fallback_price_sel:
                                    el = await card.query_selector(config.fallback_price_sel)
                                price_raw = (await el.inner_text()).strip() if el else ""
                                price = parse_tr_price(price_raw)

                            if not product_name or price <= 0:
                                continue

                            # Skip template placeholders (BizimToptan jQuery tmpl)
                            if "${" in product_name:
                                continue

                            # ── Link ──────────────────────────────────────────
                            if config.link_via_parent:
                                href = await card.evaluate(
                                    "el => el.closest('a') ? el.closest('a').getAttribute('href') : ''"
                                ) or ""
                            elif config.link_sel:
                                link_el = await card.query_selector(config.link_sel)
                                href = (await link_el.get_attribute("href") or "") if link_el else ""
                                if href and ("${" in href or href.startswith("javascript")):
                                    href = ""
                            else:
                                href = ""

                            product_url = _absolute(href, config.base_url, base_url)

                            # url_param duplicate guard: if page 2 starts with same
                            # product as page 1, site has no real page 2
                            if config.pagination == URL_PARAM:
                                if page_num == 1 and page_count == 0:
                                    first_product_name = product_name
                                elif page_num > 1 and page_count == 0:
                                    if product_name == first_product_name:
                                        logger.info(f"{name}: page {page_num} repeats page 1 — stopping")
                                        cards = []
                                        break

                            products.append({
                                "product_name":  product_name,
                                "current_price": price,
                                "market_name":   config.market_name,
                                "product_url":   product_url,
                            })
                            page_count += 1

                        except Exception as exc:
                            logger.debug(f"{name} card error: {exc}")

                    if not cards:
                        break

                    url_total += page_count
                    logger.info(f"{name}: page {page_num} -> {page_count} products")

                    # ── Advance to next page ───────────────────────────────────
                    if config.pagination == NEXT_BUTTON:
                        btn = await page.query_selector(config.next_btn_sel)
                        if not btn:
                            break
                        css = await btn.get_attribute("class") or ""
                        if "disabled" in css.lower():
                            break
                        await btn.click()
                        await page.wait_for_load_state("networkidle", timeout=15_000)
                        await asyncio.sleep(2)
                        page_num += 1
                    elif config.pagination == URL_PARAM:
                        page_num += 1
                        await asyncio.sleep(1)
                    else:
                        break  # NONE — single page only

                    if page_num > config.max_pages:
                        logger.warning(f"{name}: hit {config.max_pages}-page cap for {base_url}")
                        break

                logger.info(f"{name}: {url_total} products total from {base_url}")
                await page.close()
                await asyncio.sleep(1)

            except Exception as exc:
                logger.error(f"{name} page error for {base_url}: {repr(exc)}")

        await browser.close()

    logger.info(f"{name} scrape complete: {len(products)} total products")
    return products
