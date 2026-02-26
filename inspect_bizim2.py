import asyncio
from playwright.async_api import async_playwright

async def inspect():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
        )
        page = await context.new_page()
        
        url = "https://www.bizimtoptan.com.tr/kampanyalar"
        print(f"Loading {url} ...")
        await page.goto(url, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)
        
        try:
            await page.wait_for_function(
                """() => {
                    const els = document.querySelectorAll('.productbox-name');
                    return Array.from(els).some(el => el.innerText && !el.innerText.includes('${'));
                }""",
                timeout=10000,
            )
        except Exception:
            pass
        
        cards = await page.query_selector_all(".product-box-container")
        print(f"Total cards: {len(cards)}")
        
        products = []
        for i, card in enumerate(cards[:20]):  # inspect first 20
            name_el = await card.query_selector(".productbox-name")
            price_el = await card.query_selector(".campaign-price")
            if not price_el:
                price_el = await card.query_selector(".product-price")
            link_el = await card.query_selector("a[href]")
            
            name = (await name_el.inner_text()).strip() if name_el else "NO NAME"
            price_raw = (await price_el.inner_text()).strip() if price_el else "NO PRICE"
            href = await link_el.get_attribute("href") if link_el else "NO LINK"
            
            # Check for template placeholders
            has_template = "${" in name or "${" in price_raw or (href and "${" in href)
            
            print(f"[{i}] name={name!r} | price={price_raw!r} | href={href!r} | tmpl={has_template}")
            products.append({"name": name, "price": price_raw, "href": href})
        
        # Check URL uniqueness
        hrefs = [p["href"] for p in products]
        unique_hrefs = set(hrefs)
        print(f"\nFirst 20 hrefs: {len(hrefs)} total, {len(unique_hrefs)} unique")
        if len(unique_hrefs) < 5:
            print("WARNING: Most hrefs are the same! This will cause dedup to drop them.")
        
        # Also check full HTML of first card
        if cards:
            html = await cards[0].inner_html()
            print(f"\nFirst card HTML:\n{html[:1000]}")
        
        await browser.close()

asyncio.run(inspect())
