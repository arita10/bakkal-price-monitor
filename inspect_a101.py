"""
inspect_a101.py — One-off script to discover CSS selectors on a101.com.tr/kapida pages.
Run:  python inspect_a101.py
"""

import asyncio
import sys
from playwright.async_api import async_playwright

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST_URL = "https://www.a101.com.tr/kapida/cok-al-az-ode"

CARD_SELECTORS = [
    "[class*='product-card']",
    "[class*='ProductCard']",
    "[class*='product-item']",
    "[class*='ProductItem']",
    "[class*='productCard']",
    "[class*='product-list-item']",
    "[data-testid*='product']",
    "li[class*='product']",
    "[class*='item-card']",
    "[class*='card']",
    "article",
]
NAME_SELECTORS = [
    "[class*='product-name']",
    "[class*='ProductName']",
    "[class*='productName']",
    "[class*='product-title']",
    "[class*='title']",
    "h3", "h2", "h1",
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
    "[class*='price']",
    "span[class*='price']",
    "[data-testid*='price']",
]
LINK_SELECTORS = [
    "a[class*='product']",
    "a[href*='/p/']",
    "a[href*='-p-']",
    "a[href*='kapida']",
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
        await asyncio.sleep(3)

        # Dismiss cookie consent dialog if present
        dismissed = False
        for btn_sel in [
            "#CybotCookiebotDialogBodyButtonAccept",
            "#CybotCookiebotDialogBodyLevelButtonAccept",
            "button[id*='Accept']",
            "button[id*='accept']",
            "button[class*='accept']",
            "button[class*='Accept']",
            ".cookie-accept",
            "[aria-label*='Accept']",
            "[aria-label*='Kabul']",
            "button:has-text('Kabul Et')",
            "button:has-text('Tümünü Kabul')",
            "button:has-text('Accept')",
        ]:
            try:
                btn = await page.query_selector(btn_sel)
                if btn:
                    await btn.click()
                    print(f"  Dismissed cookie dialog via {btn_sel!r}")
                    await asyncio.sleep(2)
                    dismissed = True
                    break
            except Exception:
                pass

        if not dismissed:
            # Try pressing Escape to close the dialog
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)
            # Also try hiding the dialog via JS
            try:
                await page.evaluate("""
                    const d = document.getElementById('CybotCookiebotDialog');
                    if (d) d.style.display = 'none';
                    const c = document.querySelector('.cookie-container');
                    if (c) c.style.display = 'none';
                """)
                print("  Hid cookie dialog via JS")
            except Exception:
                pass

        await asyncio.sleep(4)

        # ── 1. Card selectors ─────────────────────────────────────────────────
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
            print(body[:5000])
            await browser.close()
            return

        print(f"\n  Best card selector: {best_card!r}")
        cards = await page.query_selector_all(best_card)
        first_card = cards[0]

        outer = await first_card.evaluate("el => el.outerHTML")
        print(f"\n--- First card HTML (first 2000 chars) ---\n{outer[:2000]}")

        # ── 2. Name selectors ─────────────────────────────────────────────────
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

        # ── 3. Price selectors ────────────────────────────────────────────────
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

        # ── 4. Link selectors ─────────────────────────────────────────────────
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

        # Check parent <a>
        parent_href = await first_card.evaluate(
            "el => el.closest('a') ? el.closest('a').getAttribute('href') : null"
        )
        print(f"\n  Parent <a> href: {parent_href!r}")

        # ── 5. Pagination ─────────────────────────────────────────────────────
        print("\n--- Checking pagination ---")
        for pg_sel in [
            "[class*='pagination']", "[class*='Pagination']",
            "[class*='pager']", "[class*='Pager']",
            "nav[aria-label*='page']", "[class*='load-more']",
            "button[class*='more']", "[class*='next']",
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

        # ── 6. Infinite scroll test ───────────────────────────────────────────
        print("\n--- Testing infinite scroll (3 scrolls) ---")
        count_before = len(await page.query_selector_all(best_card))
        for _ in range(3):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(3)
        count_after = len(await page.query_selector_all(best_card))
        print(f"  Cards before: {count_before}  |  Cards after 3x scroll: {count_after}")
        if count_after > count_before:
            print("  -> Infinite scroll detected!")
        else:
            print("  -> No infinite scroll")

        # ── 7. URL pagination test ────────────────────────────────────────────
        print("\n--- Testing URL pagination patterns ---")
        for param in ["?page=2", "?sayfa=2", "?currentPage=1"]:
            url = TEST_URL + param
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(3)
            cards2 = await page.query_selector_all(best_card)
            first_name = ""
            if cards2 and best_name:
                el = await cards2[0].query_selector(best_name)
                if el:
                    first_name = (await el.inner_text()).strip()
            print(f"  {param}: {len(cards2)} cards, first={first_name!r}, url={page.url}")

        print("\n=== SUMMARY ===")
        print(f"  Card:      {best_card}")
        print(f"  Name:      {best_name}")
        print(f"  Price:     {best_price}")
        print(f"  Link:      {best_link}")
        print(f"  Parent <a>: {parent_href}")
        print(f"  Total cards on page: {len(cards)}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
