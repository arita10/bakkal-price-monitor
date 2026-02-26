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
        
        # Try to wait for tmpl render
        try:
            await page.wait_for_function(
                """() => {
                    const els = document.querySelectorAll('.productbox-name');
                    return Array.from(els).some(el => el.innerText && !el.innerText.includes('${'));
                }""",
                timeout=10000,
            )
            print("tmpl rendered OK")
        except Exception as e:
            print(f"tmpl wait failed: {e}")
        
        # Check selectors
        selectors = [
            ".product-box-container",
            ".productbox-name", 
            ".campaign-price",
            ".product-price",
            "[class*='product']",
            "[class*='card']",
            "[class*='item']",
        ]
        for sel in selectors:
            els = await page.query_selector_all(sel)
            print(f"  {sel}: {len(els)} elements found")
            if els and len(els) > 0:
                try:
                    text = await els[0].inner_text()
                    print(f"    first element text: {text[:100]!r}")
                except:
                    pass
        
        # Print page title and first 2000 chars of body HTML
        title = await page.title()
        print(f"\nPage title: {title}")
        body_html = await page.evaluate("document.body.innerHTML")
        print(f"\nFirst 3000 chars of body HTML:")
        print(body_html[:3000])
        
        await browser.close()

asyncio.run(inspect())
