"""
scraper.py — Data fetching from two sources:
  1. marketfiyati.org.tr REST API (JSON)
  2. cimri.com via Crawl4AI (HTML → Markdown)

Both sources return ProductRaw objects containing a text chunk and metadata.
The chunks are later fed to parser.py (Gemini) for structured extraction.
"""

import asyncio
import json
import logging
from dataclasses import dataclass

import requests
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from playwright.async_api import async_playwright

logger = logging.getLogger("bakkal_monitor.scraper")

# ── Target keywords for marketfiyati API ─────────────────────────────────────
# Staple groceries relevant to a Turkish Bakkal / small shop
MARKETFIYATI_KEYWORDS = [
    "süt",
    "ekmek",
    "ayçiçek yağı",
    "un",
    "şeker",
    "çay",
    "makarna",
    "pirinç",
    "peynir",
    "yumurta",
]

MARKETFIYATI_API_URL = "https://api.marketfiyati.org.tr/api/v2/search"

# ── Target pages for cimri.com Crawl4AI scraping ─────────────────────────────
CIMRI_TARGET_URLS = [
    "https://www.cimri.com/market/migros",
    "https://www.cimri.com/market",
]

# ── Target pages for essenjet.com Crawl4AI scraping ──────────────────────────
# Real category URLs discovered via browser inspection (ID/slug pattern)
ESSEN_TARGET_URLS = [
    "https://www.essenjet.com/kategori/10/Temel-Gida",
    "https://www.essenjet.com/kategori/20/Sut-Kahvaltilik",
    "https://www.essenjet.com/kategori/14/Unlu-Mamuller-Tatli",
    "https://www.essenjet.com/kategori/30/Sebze-Meyve",
    "https://www.essenjet.com/kategori/40/Et-Tavuk",
    "https://www.essenjet.com/kategori/70/Icecek",
    "https://www.essenjet.com/kategori/12/Atistirmalik",
    "https://www.essenjet.com/kategori/1000/Haftanin-Firsatlari",
]


@dataclass
class ProductRaw:
    """
    Raw content chunk ready to be sent to Gemini for parsing.
    source: 'marketfiyati_api' or 'cimri_crawl'
    content: JSON string or Markdown chunk
    source_url: originating URL (used as fallback product_url)
    """
    source: str
    content: str
    source_url: str


# ─────────────────────────────────────────────────────────────────────────────
# marketfiyati.org.tr — REST API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_marketfiyati_keyword(
    keyword: str,
    lat: float,
    lon: float,
    chunk_size: int,
) -> list[ProductRaw]:
    """
    Query marketfiyati.org.tr API for a single product keyword.
    Returns a list of ProductRaw objects (one per chunk of the JSON response).
    Returns [] on any network or HTTP error.
    """
    payload = {
        "keywords": keyword,
        "latitude": lat,
        "longitude": lon,
        "distance": 50,   # km radius — captures all major Turkish chains
        "size": 100,      # results per keyword (max for broader coverage)
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "BakkalMonitor/1.0 (price comparison tool)",
    }

    try:
        response = requests.post(
            MARKETFIYATI_API_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        content_str = json.dumps(data, ensure_ascii=False, indent=2)
        chunks = chunk_text(content_str, chunk_size)
        result = [
            ProductRaw(
                source="marketfiyati_api",
                content=chunk,
                source_url=MARKETFIYATI_API_URL,
            )
            for chunk in chunks
        ]
        logger.info(
            f"marketfiyati API: '{keyword}' → {len(result)} chunk(s)"
        )
        return result

    except requests.RequestException as exc:
        logger.error(f"marketfiyati API error for '{keyword}': {exc}")
        return []


async def fetch_all_marketfiyati(config: dict) -> list[ProductRaw]:
    """
    Sequentially query marketfiyati API for all MARKETFIYATI_KEYWORDS.
    Uses a 1-second async sleep between calls to be courteous.
    """
    all_raw: list[ProductRaw] = []
    lat = config["SHOP_LAT"]
    lon = config["SHOP_LON"]
    chunk_size = config["GEMINI_CHUNK_SIZE"]

    logger.info(
        f"Querying marketfiyati API for {len(MARKETFIYATI_KEYWORDS)} keywords "
        f"near ({lat}, {lon})"
    )

    for keyword in MARKETFIYATI_KEYWORDS:
        items = fetch_marketfiyati_keyword(keyword, lat, lon, chunk_size)
        all_raw.extend(items)
        await asyncio.sleep(1.0)

    return all_raw


# ─────────────────────────────────────────────────────────────────────────────
# cimri.com — Crawl4AI (HTML → Markdown)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_cimri(config: dict) -> list[ProductRaw]:
    """
    Scrape cimri.com market pages using Crawl4AI.
    PruningContentFilter removes navigation/footer boilerplate.
    fit_markdown is preferred over raw_markdown to save Gemini tokens.
    Returns a list of ProductRaw Markdown chunks.
    """
    chunk_size = config["GEMINI_CHUNK_SIZE"]
    results: list[ProductRaw] = []

    browser_cfg = BrowserConfig(headless=True, verbose=False)

    content_filter = PruningContentFilter(threshold=0.45)
    md_generator = DefaultMarkdownGenerator(
        content_filter=content_filter,
        options={
            "ignore_links": False,   # Keep product links
            "ignore_images": True,   # Skip image alt-text noise
            "body_width": 0,         # No line-wrapping
        },
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,          # Always fresh
        markdown_generator=md_generator,
        wait_for="css:.product-list, css:main",
        page_timeout=45_000,                  # 45 s for JS-heavy pages
    )

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for url in CIMRI_TARGET_URLS:
            try:
                logger.info(f"Crawling: {url}")
                result = await crawler.arun(url=url, config=run_cfg)

                if not result.success:
                    logger.warning(
                        f"Crawl failed for {url}: {result.error_message}"
                    )
                    continue

                markdown_text = (
                    result.markdown.fit_markdown
                    if result.markdown and result.markdown.fit_markdown
                    else (result.markdown.raw_markdown if result.markdown else "")
                )

                if not markdown_text:
                    logger.warning(f"No markdown extracted from {url}")
                    continue

                chunks = chunk_text(markdown_text, chunk_size)
                for chunk in chunks:
                    results.append(
                        ProductRaw(
                            source="cimri_crawl",
                            content=chunk,
                            source_url=url,
                        )
                    )
                logger.info(f"Crawled {url} → {len(chunks)} chunk(s)")

            except UnicodeEncodeError as enc_exc:
                # Windows terminal can't display Turkish chars in the log.
                # The content variable may still be valid — try to salvage it.
                logger.error(f"Crawl encoding error for {url} (Windows charmap)")
                try:
                    markdown_text = (
                        result.markdown.fit_markdown
                        if result.markdown and result.markdown.fit_markdown
                        else (result.markdown.raw_markdown if result.markdown else "")
                    )
                    if markdown_text:
                        chunks = chunk_text(markdown_text, chunk_size)
                        for chunk in chunks:
                            results.append(
                                ProductRaw(
                                    source="cimri_crawl",
                                    content=chunk,
                                    source_url=url,
                                )
                            )
                        logger.info(f"Salvaged {len(chunks)} chunk(s) from {url} despite encoding error")
                except Exception:
                    pass
            except Exception as exc:
                logger.error(f"Unexpected error crawling {url}: {repr(exc)}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# essenjet.com — Direct Playwright scrape (no AI parsing needed)
# Products, prices and URLs are extracted directly via CSS selectors.
# Returns ProductData objects instead of raw chunks — bypasses OpenAI.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_tr_price(raw: str) -> float:
    """
    Convert Turkish price string to float.
    '84,90 ₺'  → 84.90
    '1.249,90 ₺' → 1249.90
    """
    cleaned = raw.replace("₺", "").replace("\xa0", "").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


async def scrape_essen_direct() -> list:
    """
    Scrape essenjet.com using Playwright directly.
    Extracts product name, price, and URL via confirmed CSS selectors.
    Returns a list of dicts with keys: product_name, current_price,
    market_name, product_url. No AI parsing needed — data is structured.
    """
    products = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "tr-TR,tr;q=0.9"},
        )

        for url in ESSEN_TARGET_URLS:
            try:
                logger.info(f"Essen: scraping {url}")
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=45_000)
                await asyncio.sleep(2)

                cards = await page.query_selector_all(".urunler-col")
                if not cards:
                    logger.warning(f"Essen: no product cards at {url}")
                    await page.close()
                    continue

                page_count = 0
                for card in cards:
                    try:
                        name_el  = await card.query_selector("h6.min-height-name")
                        price_el = await card.query_selector("span.priceText")
                        link_el  = await card.query_selector('a[href*="/urun/"]')

                        if not name_el or not price_el:
                            continue

                        name  = (await name_el.inner_text()).strip()
                        price_raw = (await price_el.inner_text()).strip()
                        price = _parse_tr_price(price_raw)

                        if not name or price <= 0:
                            continue

                        href = await link_el.get_attribute("href") if link_el else ""
                        product_url = (
                            f"https://www.essenjet.com{href}"
                            if href.startswith("/")
                            else href or url
                        )

                        products.append({
                            "product_name": name,
                            "current_price": price,
                            "market_name": "Essen JET",
                            "product_url": product_url,
                        })
                        page_count += 1

                    except Exception as card_exc:
                        logger.debug(f"Essen card error: {card_exc}")

                logger.info(f"Essen: {page_count} products from {url}")
                await page.close()
                await asyncio.sleep(2)

            except Exception as exc:
                logger.error(f"Essen page error for {url}: {repr(exc)}")

        await browser.close()

    logger.info(f"Essen scrape complete: {len(products)} total products")
    return products


async def scrape_essen(_config: dict) -> list[ProductRaw]:
    """
    Compatibility shim kept for import compatibility with main.py.
    Essen uses direct structured extraction via scrape_essen_direct()
    which main.py calls separately — no raw chunks needed here.
    """
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int) -> list[str]:
    """
    Split text into chunks of at most chunk_size characters.
    Breaks at newline boundaries when possible to avoid mid-sentence splits.
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunk = text[start:]
            if chunk.strip():
                chunks.append(chunk)
            break
        # Prefer breaking at the last newline within the window
        break_point = text.rfind("\n", start, end)
        if break_point <= start:
            break_point = end
        chunk = text[start:break_point]
        if chunk.strip():
            chunks.append(chunk)
        start = break_point + 1

    return chunks
