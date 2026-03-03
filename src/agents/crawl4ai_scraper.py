"""
src/agents/crawl4ai_scraper.py — AI-assisted scrapers using Crawl4AI.

Scrapes HTML pages and converts them to Markdown chunks that are then
sent to GPT-4o Mini for structured product extraction (see parser.py).

Sources:
  scrape_cimri()  — cimri.com market comparison pages
  scrape_a101()   — a101.com.tr campaign/home pages
"""

import logging
from dataclasses import dataclass

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.content_filter_strategy import PruningContentFilter
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

from src.utils import chunk_text

logger = logging.getLogger("bakkal_monitor.agents.crawl4ai_scraper")

# ── Target URLs ───────────────────────────────────────────────────────────────

CIMRI_TARGET_URLS = [
    "https://www.cimri.com/market/migros",
    "https://www.cimri.com/market",
]

A101_TARGET_URLS = [
    "https://www.a101.com.tr/kampanyalar",
    "https://www.a101.com.tr/",
]


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ProductRaw:
    """
    Raw content chunk ready to be sent to GPT-4o Mini for parsing.
    source:     identifier string, e.g. 'cimri_crawl' or 'a101_crawl'
    content:    Markdown text chunk
    source_url: originating URL (used as fallback product_url)
    """
    source: str
    content: str
    source_url: str


# ── Shared Crawl4AI config factory ────────────────────────────────────────────

def _build_run_cfg() -> CrawlerRunConfig:
    content_filter = PruningContentFilter(threshold=0.45)
    md_generator = DefaultMarkdownGenerator(
        content_filter=content_filter,
        options={
            "ignore_links": False,
            "ignore_images": True,
            "body_width": 0,
        },
    )
    return CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        markdown_generator=md_generator,
        wait_for="css:.product-list, main",
        page_timeout=45_000,
    )


def _markdown_from_result(result) -> str:
    """Extract the best markdown text from a crawl result."""
    if result.markdown and result.markdown.fit_markdown:
        return result.markdown.fit_markdown
    if result.markdown and result.markdown.raw_markdown:
        return result.markdown.raw_markdown
    return ""


async def _crawl_urls(
    urls: list[str],
    source_tag: str,
    chunk_size: int,
) -> list[ProductRaw]:
    """
    Crawl all URLs in parallel using arun_many(), return ProductRaw chunks.
    Falls back to sequential arun() if arun_many is unavailable.
    """
    results: list[ProductRaw] = []
    browser_cfg = BrowserConfig(headless=True, verbose=False)
    run_cfg = _build_run_cfg()

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        try:
            # arun_many() crawls all URLs concurrently inside one browser session
            crawl_results = await crawler.arun_many(urls=urls, config=run_cfg)
        except AttributeError:
            # Older crawl4ai versions — fall back to sequential
            crawl_results = []
            for url in urls:
                crawl_results.append(await crawler.arun(url=url, config=run_cfg))

        for url, result in zip(urls, crawl_results):
            try:
                if not result.success:
                    logger.warning(f"{source_tag}: crawl failed for {url}: {result.error_message}")
                    continue

                markdown_text = _markdown_from_result(result)
                if not markdown_text:
                    logger.warning(f"{source_tag}: no markdown extracted from {url}")
                    continue

                chunks = chunk_text(markdown_text, chunk_size)
                for chunk in chunks:
                    results.append(ProductRaw(source=source_tag, content=chunk, source_url=url))
                logger.info(f"{source_tag}: crawled {url} -> {len(chunks)} chunk(s)")

            except UnicodeEncodeError:
                logger.error(f"{source_tag}: encoding error for {url} (Windows charmap)")
                try:
                    markdown_text = _markdown_from_result(result)
                    if markdown_text:
                        chunks = chunk_text(markdown_text, chunk_size)
                        for chunk in chunks:
                            results.append(ProductRaw(source=source_tag, content=chunk, source_url=url))
                        logger.info(f"{source_tag}: salvaged {len(chunks)} chunk(s) from {url}")
                except Exception:
                    pass
            except Exception as exc:
                logger.error(f"{source_tag}: unexpected error for {url}: {repr(exc)}")

    return results


# ── Public API ────────────────────────────────────────────────────────────────

async def scrape_cimri(config: dict) -> list[ProductRaw]:
    """Scrape cimri.com market pages. Returns Markdown chunks for AI parsing."""
    return await _crawl_urls(CIMRI_TARGET_URLS, "cimri_crawl", config["GEMINI_CHUNK_SIZE"])


async def scrape_a101(config: dict) -> list[ProductRaw]:
    """Scrape a101.com.tr campaign pages. Returns Markdown chunks for AI parsing."""
    return await _crawl_urls(A101_TARGET_URLS, "a101_crawl", config["GEMINI_CHUNK_SIZE"])
