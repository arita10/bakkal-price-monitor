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
