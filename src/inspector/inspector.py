"""
src/inspector/inspector.py — Orchestrator for web inspection.

Runs the 3-step inspection pipeline for one or more URLs:
  1. detector.py       → detect JS technology
  2. selector_finder.py → find CSS selectors + sample data
  3. pagination_checker.py → detect pagination type

Returns InspectionResult (or list thereof).
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from src.browsers.playwright_browser import new_context
from src.inspector import detector, selector_finder, pagination_checker
from src.inspector.selector_finder import SelectorSet
from src.inspector.pagination_checker import PaginationInfo

logger = logging.getLogger("bakkal_monitor.inspector")


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class InspectionResult:
    url: str = ""
    site_name: str = ""          # e.g. "migros" derived from domain
    technology: str = "static"   # "angular"|"react"|"nextjs"|"vue"|"nuxt"|"jquery"|"static"
    needs_wait: bool = False
    wait_selector: str = ""
    has_cookie_dialog: bool = False
    cookie_dismiss_selector: str = ""
    selectors: SelectorSet = field(default_factory=SelectorSet)
    pagination: PaginationInfo = field(default_factory=PaginationInfo)
    sample_products: list = field(default_factory=list)
    error: str = ""              # non-empty if inspection failed


def _site_name_from_url(url: str) -> str:
    """Extract a safe identifier from the domain, e.g. 'migros' from migros.com.tr."""
    host = urlparse(url).hostname or url
    # Strip leading www. / tr. / m.
    host = re.sub(r"^(www\.|tr\.|m\.)", "", host)
    # Take first part before the TLD
    name = host.split(".")[0]
    # Sanitize to valid Python identifier chars
    name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return name or "site"


# ── Core inspector ────────────────────────────────────────────────────────────

async def _inspect_one(url: str, page) -> InspectionResult:
    """Run the full 3-step pipeline on a single already-opened page."""
    result = InspectionResult(url=url, site_name=_site_name_from_url(url))

    # ── Step 1: Detect technology ─────────────────────────────────────────────
    logger.info(f"  [1/3] Detecting technology...")
    tech = await detector.detect(page)
    result.technology = tech
    wait_needed, wait_sel = await detector.needs_wait(tech, page)
    result.needs_wait = wait_needed
    result.wait_selector = wait_sel
    logger.info(f"        Technology: {tech}")

    # If Angular / React — give extra time for components to render
    if wait_needed:
        if wait_sel:
            try:
                await page.wait_for_function(
                    f"() => document.querySelectorAll('{wait_sel}').length > 0",
                    timeout=15_000,
                )
            except Exception:
                pass
        await asyncio.sleep(3 if tech == "angular" else 2)

    # ── Step 2: Dismiss cookie dialog + find selectors ────────────────────────
    logger.info(f"  [2/3] Finding selectors...")
    dismiss_sel = await selector_finder.dismiss_cookie(page)
    if dismiss_sel:
        result.has_cookie_dialog = True
        result.cookie_dismiss_selector = dismiss_sel
        logger.info(f"        Cookie dismissed via: {dismiss_sel!r}")

    sel_set = await selector_finder.find_selectors(page)
    result.selectors = sel_set
    result.sample_products = sel_set.sample_products

    logger.info(f"        Card:  {sel_set.card!r} ({sel_set.card_count} found)")
    logger.info(f"        Name:  {sel_set.name!r}")
    logger.info(f"        Price: {sel_set.price!r}")
    logger.info(f"        Link:  {sel_set.link!r}"
                + (" (via parent)" if sel_set.link_via_parent else ""))

    # ── Step 3: Check pagination ───────────────────────────────────────────────
    logger.info(f"  [3/3] Checking pagination...")
    pag = await pagination_checker.check(page, sel_set.card, url)
    result.pagination = pag
    logger.info(f"        Pagination: {pag.type}"
                + (f" (?{pag.param_name}=N)" if pag.param_name and pag.type == "url_param" else ""))

    return result


async def _inspect_url(url: str) -> InspectionResult:
    """Open a fresh browser context for a single URL and run inspection."""
    logger.info(f"Inspecting: {url}")
    try:
        async with async_playwright() as p:
            browser, context = await new_context(p)
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60_000)
            result = await _inspect_one(url, page)
            await browser.close()
            return result
    except Exception as exc:
        logger.error(f"Inspection failed for {url}: {exc}")
        return InspectionResult(
            url=url,
            site_name=_site_name_from_url(url),
            error=str(exc),
        )


# ── Public API ────────────────────────────────────────────────────────────────

class WebInspector:
    """
    Public interface for website inspection.

    Usage (library):
        result = await WebInspector.inspect("https://www.migros.com.tr/meyve-sebze-c-2")

    Usage (multiple URLs):
        results = await WebInspector.inspect_many([url1, url2, url3])
    """

    @staticmethod
    async def inspect(url: str) -> InspectionResult:
        """Inspect a single URL. Returns InspectionResult."""
        return await _inspect_url(url)

    @staticmethod
    async def inspect_many(
        urls: list[str],
        concurrency: int = 3,
    ) -> list[InspectionResult]:
        """
        Inspect multiple URLs with bounded concurrency.

        Args:
            urls:        List of URLs to inspect.
            concurrency: Max parallel browser sessions (default 3).
        Returns:
            List of InspectionResult in the same order as input.
        """
        sem = asyncio.Semaphore(concurrency)

        async def _bounded(url: str) -> InspectionResult:
            async with sem:
                return await _inspect_url(url)

        tasks = [asyncio.create_task(_bounded(u)) for u in urls]
        return await asyncio.gather(*tasks)
