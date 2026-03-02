"""
inspect_carrefoursa.py — One-off script to discover CSS selectors on carrefoursa.com.
Run:  python inspect_carrefoursa.py
Output shows the first product's name, price, and link so you can confirm selectors.
"""

import asyncio
import sys
from playwright.async_api import async_playwright

# Force UTF-8 output on Windows terminals
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST_URL = "https://www.carrefoursa.com/meyve/c/1015"

# Candidates to probe — edit after first run if needed
CARD_SELECTORS = [
    ".product-list-item",
    ".product-item",
    "[class*='product-card']",
    "[class*='ProductCard']",
    "li.product",
    ".js-product-item",
    "[data-testid*='product']",
]
NAME_SELECTORS = [
    ".product-name",
    ".product-title",
    "[class*='product-name']",
    "[class*='ProductName']",
    "h3",
    "h2",
    "[class*='name']",
]
PRICE_SELECTORS = [
    ".discounted-price",
    ".current-price",
    ".sale-price",
    "[class*='discounted']",
    "[class*='current-price']",
    "[class*='sale']",
    ".price",
    "[class*='price']",
]
LINK_SELECTORS = [
    "a.product-link",
    "a[class*='product']",
    "a[href*='/p/']",
    "a",
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # visible so you can watch
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
        )
        page = await context.new_page()

        print(f"\nNavigating to {TEST_URL} ...")
        await page.goto(TEST_URL, wait_until="networkidle", timeout=60_000)
        await asyncio.sleep(3)  # let lazy-loaded images / JS settle

        # ── 1. Find product card selector ────────────────────────────────────
        print("\n--- Probing CARD selectors ---")
        best_card = None
        for sel in CARD_SELECTORS:
            cards = await page.query_selector_all(sel)
            print(f"  {sel!r:45s} → {len(cards)} elements")
            if cards and best_card is None:
                best_card = sel

        if not best_card:
            print("  *** No card selector matched — check page HTML manually ***")
            # Dump first 3000 chars of body for inspection
            body = await page.inner_html("body")
            print("\n--- BODY (first 3000 chars) ---")
            print(body[:3000])
            await browser.close()
            return

        print(f"\n  ✓ Best card selector: {best_card!r}")
        cards = await page.query_selector_all(best_card)
        first_card = cards[0]

        # ── 2. Find name selector ─────────────────────────────────────────────
        print("\n--- Probing NAME selectors inside first card ---")
        best_name = None
        for sel in NAME_SELECTORS:
            el = await first_card.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                print(f"  {sel!r:45s} → {text!r}")
                if best_name is None:
                    best_name = sel
            else:
                print(f"  {sel!r:45s} → (not found)")

        # ── 3. Find price selector ────────────────────────────────────────────
        print("\n--- Probing PRICE selectors inside first card ---")
        best_price = None
        for sel in PRICE_SELECTORS:
            el = await first_card.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                print(f"  {sel!r:45s} → {text!r}")
                if best_price is None:
                    best_price = sel
            else:
                print(f"  {sel!r:45s} → (not found)")

        # ── 4. Find link selector ─────────────────────────────────────────────
        print("\n--- Probing LINK selectors inside first card ---")
        best_link = None
        for sel in LINK_SELECTORS:
            el = await first_card.query_selector(sel)
            if el:
                href = await el.get_attribute("href")
                print(f"  {sel!r:45s} → href={href!r}")
                if best_link is None and href and not href.startswith("javascript"):
                    best_link = sel
            else:
                print(f"  {sel!r:45s} → (not found)")

        # ── 5. Check pagination ───────────────────────────────────────────────
        print("\n--- Checking pagination ---")
        for pg_sel in ["[class*='pagination']", "nav[aria-label*='page']", ".pager", "[class*='pager']"]:
            pg = await page.query_selector(pg_sel)
            if pg:
                text = (await pg.inner_text()).strip()[:200]
                print(f"  Found: {pg_sel!r} → {text!r}")
                # Dump all hrefs inside the pager
                links = await pg.query_selector_all("a")
                for lnk in links:
                    href = await lnk.get_attribute("href")
                    label = (await lnk.inner_text()).strip()
                    print(f"    pager link: {label!r} -> {href!r}")

        # Test ?currentPage= pattern
        await page.goto(TEST_URL + "?currentPage=1", wait_until="networkidle", timeout=30_000)
        await asyncio.sleep(2)
        cards_p1 = await page.query_selector_all("[class*='product-card']")
        print(f"\n  Cards with ?currentPage=1: {len(cards_p1)}")
        print(f"  URL: {page.url}")

        current_url = page.url
        print(f"\n  Final URL: {current_url}")

        # ── 6. Summary ────────────────────────────────────────────────────────
        print("\n=== SUMMARY ===")
        print(f"  Card:  {best_card}")
        print(f"  Name:  {best_name}")
        print(f"  Price: {best_price}")
        print(f"  Link:  {best_link}")
        print(f"  Total cards on page: {len(cards)}")

        input("\nPress Enter to close browser...")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
