"""
inspect_migros.py — One-off script to discover CSS selectors on migros.com.tr.
Run:  python inspect_migros.py
"""

import asyncio
import sys
from playwright.async_api import async_playwright

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST_URL = "https://www.migros.com.tr/meyve-sebze-c-2"

CARD_SELECTORS = [
    "sm-list-page-item",
    "[class*='product-card']",
    "[class*='ProductCard']",
    ".product-item",
    ".product-list-item",
    "[data-testid*='product']",
    "li[class*='product']",
    "[class*='product-wrapper']",
    "fe-product-card",
    "sm-product-card",
]
NAME_SELECTORS = [
    "[class*='product-name']",
    "[class*='ProductName']",
    "h3",
    "h2",
    "[class*='name']",
    "span[class*='name']",
    "p[class*='name']",
]
PRICE_SELECTORS = [
    "[class*='discounted']",
    "[class*='sale-price']",
    "[class*='current-price']",
    "[class*='price']",
    "span[class*='price']",
    "[data-testid*='price']",
]
LINK_SELECTORS = [
    "a[class*='product']",
    "a[href*='/p/']",
    "a[href*='-p-']",
    "a",
]


async def main():
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
        page = await context.new_page()

        print(f"\nNavigating to {TEST_URL} ...")
        await page.goto(TEST_URL, wait_until="networkidle", timeout=60_000)
        await asyncio.sleep(6)
        # Wait for Angular lazy components to render actual product cards
        try:
            await page.wait_for_function(
                """() => {
                    const items = document.querySelectorAll('sm-list-page-item, [class*=\\'product-card\\']');
                    return Array.from(items).length > 5;
                }""",
                timeout=20_000,
            )
        except Exception:
            print("  (wait_for_function timed out — continuing anyway)")
        await asyncio.sleep(2)

        # ── 1. Find product card selector ─────────────────────────────────────
        print("\n--- Probing CARD selectors ---")
        best_card = None
        for sel in CARD_SELECTORS:
            try:
                cards = await page.query_selector_all(sel)
                print(f"  {sel!r:50s} -> {len(cards)} elements")
                if cards and best_card is None:
                    best_card = sel
            except Exception as e:
                print(f"  {sel!r:50s} -> ERROR: {e}")

        if not best_card:
            print("  *** No card selector matched — dumping body snippet ***")
            body = await page.inner_html("body")
            print(body[:4000])
            await browser.close()
            return

        print(f"\n  Best card selector: {best_card!r}")
        cards = await page.query_selector_all(best_card)
        first_card = cards[0]

        # Also dump first card's outer HTML to see full structure
        outer = await first_card.evaluate("el => el.outerHTML")
        print(f"\n--- First card HTML (first 1500 chars) ---\n{outer[:1500]}")

        # ── 2. Find name selector ─────────────────────────────────────────────
        print("\n--- Probing NAME selectors inside first card ---")
        best_name = None
        for sel in NAME_SELECTORS:
            try:
                el = await first_card.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    print(f"  {sel!r:50s} -> {text!r}")
                    if best_name is None:
                        best_name = sel
                else:
                    print(f"  {sel!r:50s} -> (not found)")
            except Exception as e:
                print(f"  {sel!r:50s} -> ERROR: {e}")

        # ── 3. Find price selector ────────────────────────────────────────────
        print("\n--- Probing PRICE selectors inside first card ---")
        best_price = None
        for sel in PRICE_SELECTORS:
            try:
                el = await first_card.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    print(f"  {sel!r:50s} -> {text!r}")
                    if best_price is None:
                        best_price = sel
                else:
                    print(f"  {sel!r:50s} -> (not found)")
            except Exception as e:
                print(f"  {sel!r:50s} -> ERROR: {e}")

        # ── 4. Find link selector ─────────────────────────────────────────────
        print("\n--- Probing LINK selectors inside first card ---")
        best_link = None
        for sel in LINK_SELECTORS:
            try:
                el = await first_card.query_selector(sel)
                if el:
                    href = await el.get_attribute("href")
                    print(f"  {sel!r:50s} -> href={href!r}")
                    if best_link is None and href and not href.startswith("javascript"):
                        best_link = sel
                else:
                    print(f"  {sel!r:50s} -> (not found)")
            except Exception as e:
                print(f"  {sel!r:50s} -> ERROR: {e}")

        # ── 5. Pagination ─────────────────────────────────────────────────────
        print("\n--- Checking pagination ---")
        for pg_sel in [
            "[class*='pagination']", "[class*='Pagination']",
            "[class*='pager']", "nav[aria-label*='page']",
            "[class*='load-more']", "button[class*='more']",
        ]:
            try:
                pg = await page.query_selector(pg_sel)
                if pg:
                    text = (await pg.inner_text()).strip()[:200]
                    print(f"  Found: {pg_sel!r} -> {text!r}")
                    links = await pg.query_selector_all("a, button")
                    for lnk in links[:5]:
                        href = await lnk.get_attribute("href") or ""
                        label = (await lnk.inner_text()).strip()
                        print(f"    link: {label!r} -> {href!r}")
            except Exception as e:
                print(f"  {pg_sel!r} -> ERROR: {e}")

        # Test scroll-to-load (infinite scroll) — scroll down multiple times
        print("\n--- Testing infinite scroll (3 scrolls) ---")
        count_before = len(await page.query_selector_all(best_card))
        for i in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)
        count_after = len(await page.query_selector_all(best_card))
        print(f"  Cards before scroll: {count_before}")
        print(f"  Cards after 3x scroll: {count_after}")
        if count_after > count_before:
            print("  -> Infinite scroll detected!")
        else:
            print("  -> No infinite scroll (fixed page size)")

        # Also probe all price selectors on a card that has a sale price
        print("\n--- Probing ALL price selectors on all cards (looking for sale-price) ---")
        all_cards = await page.query_selector_all(best_card)
        for idx, card in enumerate(all_cards[:5]):
            sale = await card.query_selector("[class*='sale-price']")
            reg  = await card.query_selector("[class*='price']")
            sale_txt = (await sale.inner_text()).strip() if sale else "(none)"
            reg_txt  = (await reg.inner_text()).strip()  if reg  else "(none)"
            name_el  = await card.query_selector("[class*='product-name']")
            name_txt = (await name_el.inner_text()).strip() if name_el else "?"
            print(f"  card {idx}: name={name_txt!r}  sale-price={sale_txt!r}  price={reg_txt!r}")

        print("\n=== SUMMARY ===")
        print(f"  Card:  {best_card}")
        print(f"  Name:  {best_name}")
        print(f"  Price: {best_price}")
        print(f"  Link:  {best_link}")
        print(f"  Total cards on page: {len(cards)}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
