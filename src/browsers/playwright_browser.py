"""
src/browsers/playwright_browser.py — Shared Playwright browser/context factory.

All CSS-selector scrapers (bizimtoptan, carrefoursa, migros, sok, a101kapida)
use the same browser launch args and user-agent so we define them once here.
"""

from playwright.async_api import BrowserContext, Playwright, Route

LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Resource types that carry no product data — block them to cut load time
_BLOCKED_TYPES = {"image", "media", "font", "stylesheet"}

# Third-party domains that are pure tracking/analytics — block entirely
_BLOCKED_DOMAINS = {
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "facebook.net", "hotjar.com", "segment.io", "amplitude.com",
    "intercom.io", "clarity.ms",
}


async def _block_resources(route: Route) -> None:
    """Abort requests for resources the scraper doesn't need."""
    if route.request.resource_type in _BLOCKED_TYPES:
        await route.abort()
        return
    url = route.request.url
    if any(d in url for d in _BLOCKED_DOMAINS):
        await route.abort()
        return
    await route.continue_()


async def new_context(p: Playwright, headless: bool = True) -> tuple:
    """
    Launch a headless Chromium browser and return (browser, context).
    Images, fonts, stylesheets, and analytics are blocked automatically
    to reduce page load time. Caller is responsible for closing the browser.
    """
    browser = await p.chromium.launch(
        headless=headless,
        args=LAUNCH_ARGS,
    )
    context: BrowserContext = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
    )
    await context.route("**/*", _block_resources)
    return browser, context
