"""
tools/debug_carrefour.py — Diagnostic script for CarrefourSA scraper.

Run:  python tools/debug_carrefour.py

Visits a single CarrefourSA category page and prints:
  - How many cards each candidate selector finds
  - Sample name / price / link text from the first matching card
  - What the next-button selector finds
"""

import asyncio
import sys
import os

# Make sure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.async_api import async_playwright
from src.browsers.playwright_browser import new_context

TEST_URL = "https://www.carrefoursa.com/meyve/c/1015"

# Candidate card selectors to probe
CARD_CANDIDATES = [
    "[class*='product-card']",
    "[class*='productCard']",
    "[class*='ProductCard']",
    "[class*='product-item']",
    "[class*='productItem']",
    "div[class*='product']",
    "li[class*='product']",
    "article[class*='product']",
    ".product-card",
    ".product-item",
]

# Candidate name selectors (inside card)
NAME_CANDIDATES = [
    "h3", "h2",
    "[class*='product-name']",
    "[class*='productName']",
    "[class*='name']",
    "p[class*='name']",
    "span[class*='name']",
]

# Candidate price selectors (inside card)
PRICE_CANDIDATES = [
    "[class*='discounted']",
    "[class*='discount']",
    "[class*='sale-price']",
    "[class*='salePrice']",
    "[class*='price']",
    "[class*='Price']",
    "span[class*='price']",
    "div[class*='price']",
    "p[class*='price']",
]

# Candidate next-button selectors
NEXT_CANDIDATES = [
    "[class*='pager'] a.next",
    "[class*='pager'] a[class*='next']",
    "[class*='pager'] li.next > a",
    "[class*='next-page'] a",
    "a[class*='next']",
    "button[class*='next']",
    "[aria-label*='next' i]",
    "[aria-label*='sonraki' i]",
    "li.next a",
    ".pagination a[rel='next']",
    "[class*='pagination'] [class*='next']",
    "[class*='Pagination'] [class*='next']",
]


async def main():
    print(f"\nDiagnosing CarrefourSA: {TEST_URL}\n{'='*60}")

    async with async_playwright() as p:
        browser, context = await new_context(p)
        page = await context.new_page()

        print("Loading page...")
        await page.goto(TEST_URL, wait_until="domcontentloaded", timeout=60_000)
        # Wait for product cards to appear
        try:
            await page.wait_for_selector(".product-card", timeout=15_000)
        except Exception:
            pass
        await asyncio.sleep(4)   # let jQuery/lazy-load finish

        # ── 1. Find best card selector ────────────────────────────────────────
        print("\n[1] Card selector candidates:")
        best_card_sel = None
        best_card_count = 0
        for sel in CARD_CANDIDATES:
            count = await page.evaluate(
                f"() => document.querySelectorAll({repr(sel)}).length"
            )
            flag = " <--" if count > 5 else ""
            print(f"   {count:3d}  {sel}{flag}")
            if count > best_card_count:
                best_card_count = count
                best_card_sel = sel

        print(f"\n   Best card selector: {best_card_sel!r} ({best_card_count} cards)")

        if best_card_count == 0:
            print("\n   !! No product cards found at all.")
            print("   Dumping page title and first 2000 chars of body text:")
            title = await page.title()
            body  = await page.evaluate("() => document.body.innerText.slice(0, 2000)")
            print(f"   Title: {title}")
            print(f"   Body:\n{body}")
            await browser.close()
            return

        # ── 2. Find best name selector ────────────────────────────────────────
        print(f"\n[2] Name selector candidates (inside .product-card):")
        for sel in NAME_CANDIDATES:
            text = await page.evaluate(f"""() => {{
                const card = document.querySelector('.product-card');
                if (!card) return '';
                const el = card.querySelector({repr(sel)});
                return el ? (el.innerText || '').trim().slice(0, 60) : '';
            }}""")
            safe = text.encode('ascii', errors='replace').decode()
            flag = " OK" if text and len(text) > 3 else ""
            print(f"   {repr(sel):40s}  {repr(safe)}{flag}")

        # ── 3. Find best price selector ────────────────────────────────────────
        print(f"\n[3] Price selector candidates (inside .product-card):")
        for sel in PRICE_CANDIDATES:
            text = await page.evaluate(f"""() => {{
                const card = document.querySelector('.product-card');
                if (!card) return '';
                const el = card.querySelector({repr(sel)});
                return el ? (el.innerText || '').trim().slice(0, 60) : '';
            }}""")
            safe = text.encode('ascii', errors='replace').decode()
            flag = " OK" if text and any(c.isdigit() for c in text) else ""
            print(f"   {repr(sel):40s}  {repr(safe)}{flag}")

        # ── 4. Write first .product-card HTML to file ─────────────────────────
        print(f"\n[4] First .product-card outerHTML -> tools/carrefour_card.html")
        html = await page.evaluate("""() => {
            const card = document.querySelector('.product-card');
            return card ? card.outerHTML : 'NOT FOUND';
        }""")
        with open("tools/carrefour_card.html", "w", encoding="utf-8") as f:
            f.write(html)
        print(f"    Written {len(html)} chars")

        # Also dump inner text of first card
        inner = await page.evaluate("""() => {
            const card = document.querySelector('.product-card');
            return card ? card.innerText : '';
        }""")
        print(f"    innerText (ASCII-safe): {inner.encode('ascii', errors='replace').decode()[:300]}")

        # ── 5. Next-button selector ───────────────────────────────────────────
        print(f"\n[5] Next-button candidates:")
        for sel in NEXT_CANDIDATES:
            found = await page.evaluate(
                f"() => !!document.querySelector({repr(sel)})"
            )
            flag = " OK" if found else ""
            print(f"   {'found' if found else '     '}  {sel}{flag}")

        # ── 6. Page source hint ────────────────────────────────────────────────
        print(f"\n[6] Technology hints:")
        hints = await page.evaluate("""() => ({
            angular:  !!window.ng,
            nextjs:   !!window.__NEXT_DATA__,
            react:    !!window.__REACT_DEVTOOLS_GLOBAL_HOOK__,
            jquery:   !!window.jQuery,
            appRoot:  !!document.querySelector('app-root'),
            nextRoot: !!document.querySelector('#__next'),
        })""")
        for k, v in hints.items():
            print(f"   {k}: {v}")

        await browser.close()

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
