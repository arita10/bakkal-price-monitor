"""
src/browsers/playwright_browser.py — Shared Playwright browser/context factory.

All CSS-selector scrapers (bizimtoptan, carrefoursa, migros, sok, a101kapida)
use the same browser launch args and user-agent so we define them once here.
"""

from playwright.async_api import BrowserContext, Playwright


LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def new_context(p: Playwright, headless: bool = True) -> tuple:
    """
    Launch a headless Chromium browser and return (browser, context).
    Caller is responsible for closing the browser when done.
    """
    browser = await p.chromium.launch(
        headless=headless,
        args=LAUNCH_ARGS,
    )
    context: BrowserContext = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
    )
    return browser, context
