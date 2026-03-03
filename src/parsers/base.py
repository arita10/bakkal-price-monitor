"""
src/parsers/base.py — Common scrape engine shared by all shop scrapers.

Defines ShopConfig (per-shop selectors + pagination) and
scrape_shop() which runs the full Playwright scrape loop for any shop.

Concurrency model:
  - scrape_shop() opens ONE browser, then runs up to `url_concurrency`
    category URLs in parallel using separate pages within the same context.
  - NEXT_BUTTON pagination can't be parallelised (stateful page clicks),
    so it always runs sequentially regardless of url_concurrency.
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
    url_concurrency: int       = 4             # parallel pages per shop (tune per site)
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


# ── Card extractor (shared between sequential and parallel paths) ─────────────

async def _extract_cards(page, config: ShopConfig, base_url: str) -> list[dict]:
    """
    Extract all products from cards currently visible on the page.

    For standard configs (no custom extract_name/extract_price hooks) we use a
    single page.evaluate() call that runs entirely in the browser process —
    no per-card round-trips. This is 5-10x faster on pages with 30+ cards.

    Custom hooks (A101 Kapıda) fall back to the per-card Python loop.
    """
    results = []

    # ── Fast path: JS batch evaluation (no custom hooks needed) ───────────────
    if not config.extract_name and not config.extract_price:
        name_sel      = config.name_sel or ""
        price_sel     = config.price_sel or ""
        fallback_sel  = config.fallback_price_sel or ""
        link_sel      = config.link_sel or ""
        link_via      = config.link_via_parent

        raw = await page.evaluate(f"""() => {{
            const cards = document.querySelectorAll({repr(config.card_sel)});
            return Array.from(cards).map(card => {{
                const nameEl  = {repr(name_sel)}  ? card.querySelector({repr(name_sel)})  : null;
                let   priceEl = {repr(price_sel)} ? card.querySelector({repr(price_sel)}) : null;
                if (!priceEl && {repr(fallback_sel)})
                    priceEl = card.querySelector({repr(fallback_sel)});
                let   linkEl  = {repr(link_sel)}  ? card.querySelector({repr(link_sel)})  : null;
                let   href    = '';
                if ({'true' if link_via else 'false'}) {{
                    const a = card.closest('a');
                    href = a ? (a.getAttribute('href') || '') : '';
                }} else if (linkEl) {{
                    href = linkEl.getAttribute('href') || '';
                    if (href.includes('${{') || href.startsWith('javascript')) href = '';
                }}
                return {{
                    name:  nameEl  ? (nameEl.innerText  || '').trim() : '',
                    price: priceEl ? (priceEl.innerText || '').trim() : '',
                    href:  href,
                }};
            }});
        }}""")

        for row in raw:
            name      = row["name"]
            price_raw = row["price"]
            href      = row["href"]

            if not name or "${" in name:
                continue
            price = parse_tr_price(price_raw)
            if price <= 0:
                continue

            product_url = _absolute(href, config.base_url, base_url)
            results.append({
                "product_name":  name,
                "current_price": price,
                "market_name":   config.market_name,
                "product_url":   product_url,
                "_first_name":   results[0]["product_name"] if results else name,
            })
        return results

    # ── Slow path: per-card Python loop (only for custom hook configs) ─────────
    cards = await page.query_selector_all(config.card_sel)
    for card in cards:
        try:
            if config.extract_name:
                product_name = await config.extract_name(card)
            else:
                el = await card.query_selector(config.name_sel)
                product_name = (await el.inner_text()).strip() if el else ""

            if config.extract_price:
                price = await config.extract_price(card)
            else:
                el = await card.query_selector(config.price_sel)
                if not el and config.fallback_price_sel:
                    el = await card.query_selector(config.fallback_price_sel)
                price_raw = (await el.inner_text()).strip() if el else ""
                price = parse_tr_price(price_raw)

            if not product_name or price <= 0 or "${" in product_name:
                continue

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
            results.append({
                "product_name":  product_name,
                "current_price": price,
                "market_name":   config.market_name,
                "product_url":   product_url,
                "_first_name":   results[0]["product_name"] if results else product_name,
            })
        except Exception as exc:
            logger.debug(f"{config.market_name} card error: {exc}")

    return results


# ── Single-URL scraper (one page object, all pagination for that URL) ─────────

async def _scrape_one_url(context, config: ShopConfig, base_url: str) -> list[dict]:
    """Scrape all pages of a single category URL. Returns list of product dicts."""
    name = config.market_name
    products: list[dict] = []
    page = await context.new_page()

    try:
        await page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)

        if config.pre_scrape_hook:
            await config.pre_scrape_hook(page)

        page_num = 1
        first_product_page1 = None

        while page_num <= config.max_pages:
            if config.pagination == URL_PARAM and page_num > 1:
                url = _page_url(base_url, config.page_param, page_num)
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                if config.pre_scrape_hook:
                    await config.pre_scrape_hook(page)

            batch = await _extract_cards(page, config, base_url)
            if not batch:
                logger.info(f"{name}: no cards on page {page_num} of {base_url} — stopping")
                break

            # url_param duplicate guard
            if config.pagination == URL_PARAM and batch:
                if page_num == 1:
                    first_product_page1 = batch[0]["product_name"]
                elif batch[0]["product_name"] == first_product_page1:
                    logger.info(f"{name}: page {page_num} repeats page 1 — stopping")
                    break

            # Strip internal helper key before storing
            for p in batch:
                p.pop("_first_name", None)
            products.extend(batch)
            logger.info(f"{name}: {base_url} page {page_num} → {len(batch)} products")

            # ── Advance ────────────────────────────────────────────────────────
            if config.pagination == NEXT_BUTTON:
                btn = await page.query_selector(config.next_btn_sel)
                if not btn:
                    break
                css = await btn.get_attribute("class") or ""
                if "disabled" in css.lower():
                    break
                prev_count = len(batch)
                await btn.click()
                # Wait until the card count changes (new products loaded)
                # rather than waiting for all network requests to settle.
                try:
                    await page.wait_for_function(
                        f"() => document.querySelectorAll({repr(config.card_sel)}).length !== {prev_count}",
                        timeout=10_000,
                    )
                except Exception:
                    pass  # timeout = no new cards, outer loop will break
                page_num += 1
            elif config.pagination == URL_PARAM:
                page_num += 1
            else:
                break  # NONE — single page

            if page_num > config.max_pages:
                logger.warning(f"{name}: hit {config.max_pages}-page cap for {base_url}")
                break

    except Exception as exc:
        logger.error(f"{name} error for {base_url}: {repr(exc)}")
    finally:
        await page.close()

    return products


# ── Core engine ───────────────────────────────────────────────────────────────

async def scrape_shop(config: ShopConfig) -> list[dict]:
    """
    Run the full scrape loop for one shop using its ShopConfig.

    Category URLs are scraped in parallel (up to config.url_concurrency at once).
    NEXT_BUTTON sites are always sequential (stateful page clicks).
    Returns list[dict]: product_name, current_price, market_name, product_url.
    """
    from playwright.async_api import async_playwright
    from src.browsers.playwright_browser import new_context

    name = config.market_name

    # NEXT_BUTTON: each URL's pages must be sequential (stateful clicks),
    # but *different* URLs are fully independent — parallelise those too.
    concurrency = config.url_concurrency

    async with async_playwright() as p:
        browser, context = await new_context(p)

        # ── Cookie dismiss on first URL ────────────────────────────────────────
        if config.cookie_sel and config.target_urls:
            try:
                probe = await context.new_page()
                await probe.goto(config.target_urls[0], wait_until="domcontentloaded", timeout=60_000)
                btn = await probe.query_selector(config.cookie_sel)
                if btn:
                    await btn.click()
                    await asyncio.sleep(1.5)
                    logger.debug(f"{name}: cookie dismissed")
                await probe.close()
            except Exception:
                pass

        # ── Parallel URL scraping ─────────────────────────────────────────────
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(url: str) -> list[dict]:
            async with sem:
                return await _scrape_one_url(context, config, url)

        tasks = [asyncio.create_task(_bounded(u)) for u in config.target_urls]
        results = await asyncio.gather(*tasks)

        await browser.close()

    products = [p for batch in results for p in batch]
    logger.info(f"{name} scrape complete: {len(products)} total products")
    return products
