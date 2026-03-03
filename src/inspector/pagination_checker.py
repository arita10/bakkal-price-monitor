"""
src/inspector/pagination_checker.py — Detect how a site loads more products.

Checks in order:
  1. Infinite scroll  — scroll 3 times, count cards before/after
  2. URL param        — try ?page=2, ?sayfa=2, ?p=2
  3. Next button      — look for pager/next elements
  4. None             — all products on one page
"""

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("bakkal_monitor.inspector.pagination")

NEXT_BUTTON_SELECTORS = [
    "[class*='pager'] a.next",
    "[class*='pager'] a[class*='next']",
    "[class*='pager'] li.next > a",
    "[class*='next-page'] a",
    "[class*='pagination'] a[class*='next']",
    "[class*='Pagination'] a[class*='next']",
    "button[class*='next']",
    "[aria-label='Next']",
    "[aria-label='Sonraki']",
]

URL_PARAMS_TO_TRY = ["page", "sayfa", "p", "currentPage", "pg"]


@dataclass
class PaginationInfo:
    type: str = "none"          # "none" | "url_param" | "next_button" | "infinite_scroll"
    param_name: str = ""        # e.g. "page" or "sayfa"
    max_pages: int = 10         # safety cap for scraper


async def check(page, card_selector: str, base_url: str) -> PaginationInfo:
    """
    Detect pagination type on the current page.
    Returns PaginationInfo with type and relevant details.
    """
    result = PaginationInfo()

    if not card_selector:
        return result

    # ── 1. Infinite scroll test ───────────────────────────────────────────────
    try:
        count_before = await page.evaluate(
            f"() => document.querySelectorAll({repr(card_selector)}).length"
        )
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2.5)
        count_after = await page.evaluate(
            f"() => document.querySelectorAll({repr(card_selector)}).length"
        )
        if count_after > count_before + 2:
            logger.debug(f"Infinite scroll: {count_before} -> {count_after} cards")
            result.type = "infinite_scroll"
            result.max_pages = 1
            return result
    except Exception:
        pass

    # ── 2. URL param test ─────────────────────────────────────────────────────
    # Get name of the first card on page 1 for comparison
    first_name_p1 = ""
    try:
        first_card = await page.query_selector(card_selector)
        if first_card:
            first_name_p1 = (await first_card.inner_text()).strip()[:50]
    except Exception:
        pass

    for param in URL_PARAMS_TO_TRY:
        sep = "&" if "?" in base_url else "?"
        test_url = f"{base_url}{sep}{param}=2"
        try:
            await page.goto(test_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(2)
            cards = await page.query_selector_all(card_selector)
            if cards:
                first_name_p2 = (await cards[0].inner_text()).strip()[:50]
                # Different first card = real pagination
                if first_name_p2 and first_name_p2 != first_name_p1:
                    logger.debug(f"URL param pagination: ?{param}=N")
                    result.type = "url_param"
                    result.param_name = param
                    result.max_pages = 20
                    # Navigate back to original URL
                    await page.goto(base_url, wait_until="networkidle", timeout=30_000)
                    return result
        except Exception:
            pass

    # Navigate back to original URL before checking next button
    try:
        await page.goto(base_url, wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(1.5)
    except Exception:
        pass

    # ── 3. Next button test ───────────────────────────────────────────────────
    for sel in NEXT_BUTTON_SELECTORS:
        try:
            btn = await page.query_selector(sel)
            if btn:
                css_class = await btn.get_attribute("class") or ""
                if "disabled" not in css_class.lower():
                    logger.debug(f"Next button found: {sel!r}")
                    result.type = "next_button"
                    result.param_name = sel     # store selector for generator
                    result.max_pages = 20
                    return result
        except Exception:
            pass

    # ── 4. None — all products on one page ────────────────────────────────────
    logger.debug("No pagination detected")
    result.type = "none"
    result.max_pages = 1
    return result
