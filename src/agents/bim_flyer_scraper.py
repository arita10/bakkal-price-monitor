"""
src/agents/bim_flyer_scraper.py — BIM weekly flyer scraper using GPT-4o Vision.

How it works:
  1. Playwright visits bim.com.tr/afisler page
  2. Collects all flyer image URLs (cdn1.bim.com.tr/uploads/afisler/*.jpg)
  3. Downloads each image and encodes to base64
  4. Sends each image to GPT-4o Vision API
  5. GPT-4o reads the Turkish flyer and extracts product names + prices
  6. Returns list[dict] in the same format as all other scrapers

Note: BIM publishes weekly deals as poster/image files (afiş) instead of
      a regular product listing page, so CSS selectors cannot be used.
"""

import asyncio
import base64
import json
import logging

import httpx
from openai import OpenAI

logger = logging.getLogger("bakkal_monitor.agents.bim_flyer")

BIM_FLYER_PAGE = "https://www.bim.com.tr/Categories/680/afisler.aspx"
MARKET_NAME    = "BIM"

# GPT-4o Vision prompt — asks for structured JSON output
_VISION_PROMPT = """Bu görsel bir BIM marketi haftalık indirim afişidir (Türkçe).

Afişte gördüğün TÜM ürünleri ve fiyatları çıkar.

Kurallar:
- Türkçe ondalık ayracı VIRGÜL: "12,99 TL" → 12.99
- Türkçe binlik ayraç NOKTA: "1.249,99 TL" → 1249.99
- Sadece indirimli/kampanya fiyatını al (varsa), yoksa normal fiyatı al
- Ürün adı ve fiyat çıkarılamıyorsa o ürünü atla

Şu formatta JSON döndür:
{"products": [{"product_name": "...", "current_price": 0.0}]}
Ürün bulunamazsa: {"products": []}
"""


# ── Image collection ──────────────────────────────────────────────────────────

async def _collect_flyer_urls() -> list[str]:
    """
    Visit the BIM afisler page with Playwright and collect all flyer image URLs.
    Returns list of absolute JPG/PNG URLs.
    """
    from playwright.async_api import async_playwright
    from src.browsers.playwright_browser import new_context

    urls: list[str] = []

    async with async_playwright() as p:
        browser, context = await new_context(p)
        page = await context.new_page()

        try:
            await page.goto(BIM_FLYER_PAGE, wait_until="domcontentloaded", timeout=60_000)
            # Wait for fancybox images to appear
            try:
                await page.wait_for_selector("img.fancybox-image", timeout=15_000)
            except Exception:
                pass

            # Extract all fancybox image src attributes
            raw_urls = await page.evaluate("""() => {
                const imgs = document.querySelectorAll('img.fancybox-image');
                return Array.from(imgs).map(img => img.getAttribute('src') || '');
            }""")

            for src in raw_urls:
                if not src:
                    continue
                # Make absolute URL
                if src.startswith("http"):
                    urls.append(src)
                else:
                    urls.append("https://www.bim.com.tr" + src)

            # Fallback: also look for anchor hrefs pointing to flyer images
            if not urls:
                href_urls = await page.evaluate("""() => {
                    const links = document.querySelectorAll('a[href*="afisler"]');
                    return Array.from(links).map(a => a.getAttribute('href') || '');
                }""")
                for href in href_urls:
                    if href and any(href.endswith(ext) for ext in (".jpg", ".jpeg", ".png")):
                        if href.startswith("http"):
                            urls.append(href)
                        else:
                            urls.append("https://www.bim.com.tr" + href)

        except Exception as exc:
            logger.error(f"BIM: error collecting flyer URLs: {exc}")
        finally:
            await page.close()
            await browser.close()

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    logger.info(f"BIM: found {len(unique)} flyer image(s)")
    return unique


# ── Image download ────────────────────────────────────────────────────────────

async def _download_image(url: str) -> bytes | None:
    """Download an image from URL, return raw bytes or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
    except Exception as exc:
        logger.warning(f"BIM: failed to download image {url}: {exc}")
        return None


def _to_base64(image_bytes: bytes) -> str:
    """Encode image bytes to base64 string for OpenAI Vision API."""
    return base64.b64encode(image_bytes).decode("utf-8")


# ── GPT-4o Vision parsing ─────────────────────────────────────────────────────

def _parse_flyer_image(
    image_b64: str,
    image_url: str,
    client: OpenAI,
) -> list[dict]:
    """
    Send one flyer image to GPT-4o Vision and return list of product dicts.
    Returns [] on any failure.
    """
    try:
        response = client.chat.completions.create(
            model="gpt-4o",         # Vision requires gpt-4o (not mini)
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_b64}",
                                "detail": "high",   # high = read small text on flyers
                            },
                        },
                        {
                            "type": "text",
                            "text": _VISION_PROMPT,
                        },
                    ],
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=4096,
        )

        raw_json = response.choices[0].message.content or ""
        data = json.loads(raw_json)

        products = []
        for item in data.get("products", []):
            name  = str(item.get("product_name", "")).strip()
            price = item.get("current_price", 0)
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = 0.0

            if not name or price <= 0:
                continue

            products.append({
                "product_name":  name,
                "current_price": price,
                "market_name":   MARKET_NAME,
                "product_url":   image_url,   # no individual product page on BIM
            })

        logger.info(f"BIM Vision: {len(products)} product(s) from {image_url}")
        return products

    except Exception as exc:
        logger.error(f"BIM Vision error for {image_url}: {exc}")
        return []


# ── Public API ────────────────────────────────────────────────────────────────

async def scrape_bim_flyers(config: dict) -> list[dict]:
    """
    Full BIM flyer scrape:
      1. Collect flyer image URLs from the BIM afisler page
      2. Download + Vision-parse each image concurrently
      3. Return flat list[dict] ready for _add_direct() in main.py

    Requires config keys: OPENAI_API_KEY
    """
    from src.agents.parser import build_client

    client = build_client(config["OPENAI_API_KEY"])

    # Step 1: collect image URLs
    flyer_urls = await _collect_flyer_urls()
    if not flyer_urls:
        logger.warning("BIM: no flyer images found — skipping")
        return []

    # Step 2: download all images concurrently
    image_bytes_list = await asyncio.gather(
        *[_download_image(url) for url in flyer_urls]
    )

    # Step 3: parse each image with GPT-4o Vision (run in thread pool to avoid blocking)
    all_products: list[dict] = []
    loop = asyncio.get_running_loop()

    for url, img_bytes in zip(flyer_urls, image_bytes_list):
        if not img_bytes:
            continue
        image_b64 = _to_base64(img_bytes)
        # GPT-4o is sync — run in executor so we don't block the event loop
        products = await loop.run_in_executor(
            None, _parse_flyer_image, image_b64, url, client
        )
        all_products.extend(products)

    logger.info(f"BIM flyers total: {len(all_products)} product(s) extracted")
    return all_products
