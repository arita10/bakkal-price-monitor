"""
src/inspector/selector_finder.py — Probe CSS selectors on a live page.

Tries candidate selectors for card / name / price / link and returns
the first ones that produce real (non-empty) text content.
Also validates by extracting sample data from the first 3 cards.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("bakkal_monitor.inspector.selector_finder")

# ── Candidate selector lists ──────────────────────────────────────────────────

CARD_SELECTORS = [
    "sm-list-page-item",
    "[data-product-id]",
    "[class*='ProductCard']",
    "[class*='product-card']",
    "[class*='product-item']",
    "[class*='ProductItem']",
    "[class*='product-list-item']",
    "[class*='item-card']",
    "[class*='ItemCard']",
    "[class*='product-box']",
    "[class*='productCard']",
    "[data-testid*='product']",
    "li[class*='product']",
    "article[class*='product']",
    "article",
]

NAME_SELECTORS = [
    "[class*='product-name']",
    "[class*='ProductName']",
    "[class*='productName']",
    "[class*='product-title']",
    "[class*='ProductTitle']",
    "[class*='title']",
    "h3",
    "h2",
    "h1",
    "[class*='name']",
    "span[class*='name']",
    "p[class*='name']",
]

PRICE_SELECTORS = [
    "[class*='discounted']",
    "[class*='sale-price']",
    "[class*='salePrice']",
    "[class*='current-price']",
    "[class*='currentPrice']",
    "[class*='selling-price']",
    "[class*='sellingPrice']",
    "span[class*='price']",
    "[class*='price']",
    "[data-testid*='price']",
]

LINK_SELECTORS = [
    "a[class*='product']",
    "a[href*='/p/']",
    "a[href*='-p-']",
    "a[href*='kapida']",
    "a[href*='/urun/']",
    "a[href*='/product/']",
    "a",
]

COOKIE_SELECTORS = [
    "button:has-text('Kabul Et')",
    "button:has-text('Tümünü Kabul')",
    "button:has-text('Accept All')",
    "button:has-text('Accept')",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#CybotCookiebotDialogBodyLevelButtonAccept",
    "[id*='accept'][class*='cookie']",
    "[class*='cookie'] button",
    "[class*='consent'] button",
]


@dataclass
class SelectorSet:
    card: str = ""
    name: str = ""
    price: str = ""
    link: str = ""
    link_via_parent: bool = False   # True = use el.closest('a')
    card_count: int = 0
    sample_products: list = field(default_factory=list)


async def dismiss_cookie(page) -> str:
    """Try to dismiss cookie/consent dialogs. Returns selector used or ''."""
    for sel in COOKIE_SELECTORS:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                import asyncio
                await asyncio.sleep(1.5)
                logger.debug(f"Cookie dismissed via: {sel}")
                return sel
        except Exception:
            pass
    return ""


async def find_selectors(page) -> SelectorSet:
    """
    Probe all candidate selectors on the page.
    Returns SelectorSet with the best selector for each role.
    """
    result = SelectorSet()

    # ── 1. Find card selector ─────────────────────────────────────────────────
    best_card = None
    best_count = 0
    for sel in CARD_SELECTORS:
        try:
            count = await page.evaluate(
                f"() => document.querySelectorAll({repr(sel)}).length"
            )
            logger.debug(f"  card [{sel}] -> {count}")
            if count and count > best_count:
                best_count = count
                best_card = sel
        except Exception:
            pass

    if not best_card:
        logger.warning("No card selector found")
        return result

    result.card = best_card
    result.card_count = best_count
    logger.debug(f"Best card: {best_card!r} ({best_count} found)")

    # Get the first card element for further probing
    first_card = await page.query_selector(best_card)
    if not first_card:
        return result

    # ── 2. Find name selector ─────────────────────────────────────────────────
    for sel in NAME_SELECTORS:
        try:
            el = await first_card.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text and len(text) > 2 and "${" not in text:
                    result.name = sel
                    logger.debug(f"Best name: {sel!r} -> {text!r}")
                    break
        except Exception:
            pass

    # ── 3. Find price selector ────────────────────────────────────────────────
    for sel in PRICE_SELECTORS:
        try:
            el = await first_card.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                # Validate: must contain a digit
                if text and re.search(r"\d", text):
                    result.price = sel
                    logger.debug(f"Best price: {sel!r} -> {text!r}")
                    break
        except Exception:
            pass

    # ── 4. Find link selector ─────────────────────────────────────────────────
    for sel in LINK_SELECTORS:
        try:
            el = await first_card.query_selector(sel)
            if el:
                href = await el.get_attribute("href") or ""
                if href and not href.startswith("javascript") and href != "#":
                    result.link = sel
                    logger.debug(f"Best link: {sel!r} -> {href!r}")
                    break
        except Exception:
            pass

    # If no link found inside card, check parent <a>
    if not result.link:
        try:
            parent_href = await first_card.evaluate(
                "el => el.closest('a') ? el.closest('a').getAttribute('href') : null"
            )
            if parent_href and parent_href != "#":
                result.link = "a"
                result.link_via_parent = True
                logger.debug(f"Link via parent <a>: {parent_href!r}")
        except Exception:
            pass

    # ── 5. Extract sample products for validation ─────────────────────────────
    cards = await page.query_selector_all(best_card)
    for card in cards[:3]:
        try:
            sample = {}
            if result.name:
                el = await card.query_selector(result.name)
                sample["name"] = (await el.inner_text()).strip() if el else ""
            if result.price:
                el = await card.query_selector(result.price)
                sample["price"] = (await el.inner_text()).strip() if el else ""
            if result.link and not result.link_via_parent:
                el = await card.query_selector(result.link)
                sample["href"] = (await el.get_attribute("href") or "") if el else ""
            elif result.link_via_parent:
                sample["href"] = await card.evaluate(
                    "el => el.closest('a') ? el.closest('a').getAttribute('href') : ''"
                )
            if sample.get("name") and sample.get("price"):
                result.sample_products.append(sample)
        except Exception:
            pass

    return result
