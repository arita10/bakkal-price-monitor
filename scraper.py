"""
scraper.py — Data fetching from five sources:
  1. marketfiyati.org.tr REST API (JSON)
  2. cimri.com via Crawl4AI (HTML → Markdown)
  3. a101.com.tr via Crawl4AI (HTML → Markdown)
  4. bizimtoptan.com.tr via direct Playwright CSS extraction
  5. carrefoursa.com via direct Playwright CSS extraction

Sources 1, 4, 5 return structured dicts directly — no AI needed.
Sources 2, 3 return ProductRaw chunks fed to parser.py (OpenAI) for extraction.
"""

import asyncio
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
    # --- Staples & Grains ---
    "süt", "ekmek", "ayçiçek yağı", "un", "şeker", "çay", "makarna", "pirinç",
    "fasulye", "mercimek", "bulgur", "salça", "yufka", "arpa şehriye", 
    "hazır çorba", "mısır yağı", "tuz", "irmik", "nişasta", "karabiber", "pul biber",

    # --- Breakfast & Dairy ---
    "peynir", "yumurta", "zeytin", "margarin", "bal", "reçel", "sucuk", "yoğurt", 
    "ayran", "tereyağı", "tahin pekmez", "labne peyniri", "kaşar peyniri", 
    "süzme peynir", "salam", "sosis", "zeytin ezmesi", "kaymak",

    # --- Beverages ---
    "su", "kola", "meyve suyu", "kahve", "maden suyu", "gazoz", "türk kahvesi", 
    "limonata", "toz içecek", "meyveli soda", "şalgam suyu", "buzlu çay",

    # --- Snacks & Sweets ---
    "bisküvi", "gofret", "kek", "cips", "çikolata", "sakız", "lolipop şeker", 
    "ayçekirdeği", "fıstık", "leblebi", "kraker", "helva", "pötibör bisküvi",

    # --- Hygiene & Cleaning ---
    "deterjan", "sabun", "tuvalet kağıdı", "çamaşır suyu", "bulaşık süngeri", 
    "sıvı bulaşık deterjanı", "yüzey temizleyici", "ıslak mendil", "kağıt peçete", 
    "şampuan", "diş macunu", "tıraş bıçağı", "yumuşatıcı", "sıvı sabun", "katı sabun",

    # --- Household & Miscellaneous ---
    "kalem pil", "ince pil", "mutfak çakmağı", "kibrit", "alüminyum folyo", 
    "streç film", "çöp torbası", "ampul", "yara bandı", "traş köpüğü"
]

MARKETFIYATI_API_URL = "https://api.marketfiyati.org.tr/api/v2/search"

# ── Target pages for cimri.com Crawl4AI scraping ─────────────────────────────
CIMRI_TARGET_URLS = [
    "https://www.cimri.com/market/migros",
    "https://www.cimri.com/market",
]

# ── Target pages for a101.com.tr Crawl4AI scraping ───────────────────────────
A101_TARGET_URLS = [
    "https://www.a101.com.tr/kampanyalar",
    "https://www.a101.com.tr/",
]

# ── Target pages for bizimtoptan.com.tr scraping ─────────────────────────────
# JS-rendered product grid — scraped via Playwright (same as Essen JET)
BIZIMTOPTAN_TARGET_URLS = [
    "https://www.bizimtoptan.com.tr/kampanyalar",
    "https://www.bizimtoptan.com.tr/indirimli-urunler",
]

# ── Target category pages for carrefoursa.com scraping ───────────────────────
# JS-rendered product cards — pagination via Next button click.
# Confirmed selectors: [class*='product-card'], h3, [class*='discounted'], a[class*='product']
CARREFOURSA_TARGET_URLS = [
    "https://www.carrefoursa.com/meyve/c/1015",
    "https://www.carrefoursa.com/sebze/c/1025",
    "https://www.carrefoursa.com/sarkuteri/c/1070",
    "https://www.carrefoursa.com/kirmizi-et/c/1045",
    "https://www.carrefoursa.com/balik-ve-deniz-mahsulleri/c/1098",
    "https://www.carrefoursa.com/beyaz-et/c/1076",
    "https://www.carrefoursa.com/sut/c/1311",
    "https://www.carrefoursa.com/peynir/c/1318",
    "https://www.carrefoursa.com/yogurt/c/1389",
    "https://www.carrefoursa.com/tereyag-ve-margarin/c/1348",
    "https://www.carrefoursa.com/krema-ve-kaymak/c/1385",
    "https://www.carrefoursa.com/sutlu-tatli-puding/c/1962",
    "https://www.carrefoursa.com/kahvaltiliklar/c/1390",
    "https://www.carrefoursa.com/zeytin/c/1356",
    "https://www.carrefoursa.com/kahvaltilik-gevrek/c/1378",
    "https://www.carrefoursa.com/yumurtalar/c/1349",
    "https://www.carrefoursa.com/bakliyat/c/1121",
    "https://www.carrefoursa.com/makarna-ve-eriste/c/1122",
    "https://www.carrefoursa.com/sivi-yaglar/c/1111",
    "https://www.carrefoursa.com/un-ve-irmik/c/1276",
    "https://www.carrefoursa.com/seker/c/1495",
    "https://www.carrefoursa.com/pasta-malzemeleri/c/2391",
    "https://www.carrefoursa.com/konserve/c/1186",
    "https://www.carrefoursa.com/tuz-ve-baharat/c/1159",
    "https://www.carrefoursa.com/soslar/c/1209",
    "https://www.carrefoursa.com/sakizlar/c/1501",
    "https://www.carrefoursa.com/kuruyemis/c/1519",
    "https://www.carrefoursa.com/cipsler/c/1552",
    "https://www.carrefoursa.com/bar-ve-gofret/c/1505",
    "https://www.carrefoursa.com/biskuvi/c/1529",
    "https://www.carrefoursa.com/kuru-meyve/c/1528",
    "https://www.carrefoursa.com/sekerleme/c/1494",
    "https://www.carrefoursa.com/cikolata/c/1507",
    "https://www.carrefoursa.com/kek-ve-kruvasan/c/1545",
    "https://www.carrefoursa.com/dondurulmus-urunler-/c/1239",
    "https://www.carrefoursa.com/meze/c/1102",
    "https://www.carrefoursa.com/paketli-hazir-yemekler/c/1223",
    "https://www.carrefoursa.com/ekmek/c/2378",
    "https://www.carrefoursa.com/corekler/c/1405",
    "https://www.carrefoursa.com/kurabiyeler/c/2390",
    "https://www.carrefoursa.com/tatli/c/1058",
    "https://www.carrefoursa.com/manti-yufka-tarhana/c/1305",
    "https://www.carrefoursa.com/gazli-icecekler/c/1418",
    "https://www.carrefoursa.com/gazsiz-icecekler/c/1484",
    "https://www.carrefoursa.com/su/c/1411",
    "https://www.carrefoursa.com/maden-suyu/c/1412",
    "https://www.carrefoursa.com/sporcu-icecekleri/c/1040",
    "https://www.carrefoursa.com/cay/c/1455",
    "https://www.carrefoursa.com/kahve/c/1467",
    "https://www.carrefoursa.com/glutensiz-urunler/c/1939",
    "https://www.carrefoursa.com/organik-urunler/c/1940",
    "https://www.carrefoursa.com/barlar/c/1948",
    "https://www.carrefoursa.com/tatlandiricilar-tatlandiricili-urunler/c/1963",
    "https://www.carrefoursa.com/aktif-yasam-urunleri/c/2538",
    "https://www.carrefoursa.com/kap-dondurma/c/1261",
    "https://www.carrefoursa.com/multipack-dondurma/c/1270",
    "https://www.carrefoursa.com/tek-dondurma/c/1266",
    "https://www.carrefoursa.com/bebek-bezi/c/1875",
    "https://www.carrefoursa.com/bebek-bakim/c/1858",
    "https://www.carrefoursa.com/bebek-beslenme/c/1847",
    "https://www.carrefoursa.com/bebek-arac-gerecleri/c/1899",
    "https://www.carrefoursa.com/bebek-hijyen/c/1867",
    "https://www.carrefoursa.com/kedi/c/2055",
    "https://www.carrefoursa.com/bulasik-yikama-urunleri/c/1613",
    "https://www.carrefoursa.com/camasir-yikama-urunleri/c/1627",
    "https://www.carrefoursa.com/genel-temizlik/c/1557",
    "https://www.carrefoursa.com/oda-kokusu-ve-koku-gidericiler/c/1652",
    "https://www.carrefoursa.com/kagit-urunleri-/c/1821",
    "https://www.carrefoursa.com/ev-duzenleme/c/1992",
    "https://www.carrefoursa.com/cop-torbasi/c/1993",
    "https://www.carrefoursa.com/cilt-bakim/c/1675",
    "https://www.carrefoursa.com/makyaj-urunleri/c/1710",
    "https://www.carrefoursa.com/tiras-urunleri/c/1736",
    "https://www.carrefoursa.com/sac-bakim-urunleri/c/1757",
    "https://www.carrefoursa.com/banyo-ve-dus-urunleri/c/1772",
    "https://www.carrefoursa.com/agiz-bakim-urunleri/c/1785",
    "https://www.carrefoursa.com/parfum-deodorant/c/1805",
    "https://www.carrefoursa.com/saglik-urunleri/c/1831",
    "https://www.carrefoursa.com/gunes-koruma-urunleri/c/1838",
    "https://www.carrefoursa.com/hijyenik-ped/c/1729",
    "https://www.carrefoursa.com/kolonyalar/c/1820",
    "https://www.carrefoursa.com/agda-ve-epilasyon/c/1700",
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

# Map of marketAdi API values → display names
_MARKET_NAME_MAP = {
    "bim":          "BIM",
    "a101":         "A101",
    "sok":          "SOK",
    "migros":       "Migros",
    "carrefoursa":  "CarrefourSA",
    "hakmar":       "Hakmar",
    "tarim_kredi":  "Tarım Kredi",
    "tarım_kredi":  "Tarım Kredi",
    "onur":         "Onur Market",
    "metro":        "Metro",
    "macrocenter":  "Macrocenter",
}


def _normalize_market(raw: str) -> str:
    """Convert API market name to display name."""
    return _MARKET_NAME_MAP.get(raw.lower().strip(), raw.strip().title())


def fetch_marketfiyati_keyword(
    keyword: str,
    lat: float,
    lon: float,
) -> list[dict]:
    """
    Query marketfiyati.org.tr API for a single product keyword.
    Parses the JSON response directly — no AI needed.
    Returns a list of dicts with keys: product_name, current_price,
    market_name, product_url. Returns [] on any error.
    """
    payload = {
        "keywords": keyword,
        "latitude": lat,
        "longitude": lon,
        "distance": 50,
        "size": 100,
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

        # API returns a list directly or wrapped in a key
        items = data if isinstance(data, list) else data.get("productDepotInfoList", data.get("data", []))
        if not isinstance(items, list):
            items = []

        products = []
        for item in items:
            try:
                name = (item.get("title") or item.get("name") or "").strip()
                price_val = item.get("price") or item.get("currentPrice") or 0
                market_raw = item.get("marketAdi") or item.get("market") or item.get("depotName") or ""
                product_url = item.get("url") or item.get("productUrl") or MARKETFIYATI_API_URL

                price = float(str(price_val).replace(",", ".")) if price_val else 0.0

                if not name or price <= 0:
                    continue

                products.append({
                    "product_name": name,
                    "current_price": price,
                    "market_name": _normalize_market(market_raw),
                    "product_url": product_url,
                })
            except Exception:
                continue

        logger.info(f"marketfiyati API: '{keyword}' → {len(products)} product(s)")
        return products

    except requests.RequestException as exc:
        logger.error(f"marketfiyati API error for '{keyword}': {exc}")
        return []


async def fetch_all_marketfiyati(config: dict) -> list[dict]:
    """
    Sequentially query marketfiyati API for all MARKETFIYATI_KEYWORDS.
    Returns structured dicts directly — no OpenAI parsing needed.
    1.5 s delay between calls to respect the API rate limit.
    """
    all_products: list[dict] = []
    lat = config["SHOP_LAT"]
    lon = config["SHOP_LON"]
    total = len(MARKETFIYATI_KEYWORDS)

    logger.info(
        f"Querying marketfiyati API for {total} keywords near ({lat}, {lon})"
    )

    for i, keyword in enumerate(MARKETFIYATI_KEYWORDS, start=1):
        items = fetch_marketfiyati_keyword(keyword, lat, lon)
        all_products.extend(items)
        if i % 10 == 0 or i == total:
            logger.info(f"  marketfiyati progress: {i}/{total} keywords done, {len(all_products)} products so far")
        await asyncio.sleep(1.5)

    logger.info(f"marketfiyati API complete: {len(all_products)} product(s) total")
    return all_products


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
        wait_for="css:.product-list, main",
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
# a101.com.tr — Crawl4AI (HTML → Markdown → AI parsing)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_a101(config: dict) -> list[ProductRaw]:
    """
    Scrape a101.com.tr campaign/home pages using Crawl4AI.
    Returns a list of ProductRaw Markdown chunks for AI parsing.
    """
    chunk_size = config["GEMINI_CHUNK_SIZE"]
    results: list[ProductRaw] = []

    browser_cfg = BrowserConfig(headless=True, verbose=False)

    content_filter = PruningContentFilter(threshold=0.45)
    md_generator = DefaultMarkdownGenerator(
        content_filter=content_filter,
        options={
            "ignore_links": False,
            "ignore_images": True,
            "body_width": 0,
        },
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        markdown_generator=md_generator,
        wait_for="css:.product-list, main",
        page_timeout=45_000,
    )

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        for url in A101_TARGET_URLS:
            try:
                logger.info(f"A101: crawling {url}")
                result = await crawler.arun(url=url, config=run_cfg)

                if not result.success:
                    logger.warning(f"A101: crawl failed for {url}: {result.error_message}")
                    continue

                markdown_text = (
                    result.markdown.fit_markdown
                    if result.markdown and result.markdown.fit_markdown
                    else (result.markdown.raw_markdown if result.markdown else "")
                )

                if not markdown_text:
                    logger.warning(f"A101: no markdown extracted from {url}")
                    continue

                chunks = chunk_text(markdown_text, chunk_size)
                for chunk in chunks:
                    results.append(
                        ProductRaw(
                            source="a101_crawl",
                            content=chunk,
                            source_url=url,
                        )
                    )
                logger.info(f"A101: crawled {url} → {len(chunks)} chunk(s)")

            except UnicodeEncodeError:
                logger.error(f"A101: encoding error for {url} (Windows charmap)")
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
                                    source="a101_crawl",
                                    content=chunk,
                                    source_url=url,
                                )
                            )
                        logger.info(f"A101: salvaged {len(chunks)} chunk(s) from {url}")
                except Exception:
                    pass
            except Exception as exc:
                logger.error(f"A101: unexpected error crawling {url}: {repr(exc)}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# bizimtoptan.com.tr — Direct Playwright scrape (no AI parsing needed)
# JS-rendered product cards extracted via confirmed CSS selectors.
# Returns dicts with keys: product_name, current_price, market_name, product_url
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


async def scrape_bizimtoptan_direct() -> list:
    """
    Scrape bizimtoptan.com.tr campaign/discount pages using Playwright.
    Products are rendered via jQuery tmpl — static fetch won't work.
    Selectors confirmed: .product-box-container, .productbox-name,
    .product-price / .campaign-price, and product href links.
    Returns list of dicts.
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

        for url in BIZIMTOPTAN_TARGET_URLS:
            try:
                logger.info(f"BizimToptan: scraping {url}")
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=60_000)

                # Wait for jQuery tmpl to render real product names.
                # The template placeholder is "${item.label}" — we wait until
                # at least one .productbox-name contains a real (non-template) string.
                try:
                    await page.wait_for_function(
                        """() => {
                            const els = document.querySelectorAll('.productbox-name');
                            return Array.from(els).some(
                                el => el.innerText && !el.innerText.includes('${')
                            );
                        }""",
                        timeout=15_000,
                    )
                except Exception:
                    logger.warning(f"BizimToptan: tmpl render timeout at {url}, trying anyway")
                await asyncio.sleep(2)

                cards = await page.query_selector_all(".product-box-container")
                if not cards:
                    logger.warning(f"BizimToptan: no product cards at {url}")
                    await page.close()
                    continue

                page_count = 0
                for card in cards:
                    try:
                        name_el  = await card.query_selector(".productbox-name")
                        # Prefer campaign (discounted) price, fall back to regular
                        price_el = await card.query_selector(".campaign-price")
                        if not price_el:
                            price_el = await card.query_selector(".product-price")
                        # Use a.product-item — the real product link.
                        # a[href] picks up the wishlist button (href="javascript:;") first.
                        link_el  = await card.query_selector("a.product-item")

                        if not name_el or not price_el:
                            continue

                        name = (await name_el.inner_text()).strip()
                        price_raw = (await price_el.inner_text()).strip()

                        # Skip unrendered template placeholders
                        if "${" in name or "${" in price_raw:
                            continue

                        price = _parse_tr_price(price_raw)

                        if not name or price <= 0:
                            continue

                        href = await link_el.get_attribute("href") if link_el else ""
                        # Skip template placeholder hrefs and javascript: anchors
                        if href and ("${" in href or href.startswith("javascript")):
                            href = ""
                        if href and not href.startswith("http"):
                            product_url = f"https://www.bizimtoptan.com.tr/{href.lstrip('/')}"
                        else:
                            product_url = href or url

                        products.append({
                            "product_name": name,
                            "current_price": price,
                            "market_name": "Bizim Toptan",
                            "product_url": product_url,
                        })
                        page_count += 1

                    except Exception as card_exc:
                        logger.debug(f"BizimToptan card error: {card_exc}")

                logger.info(f"BizimToptan: {page_count} products from {url}")
                await page.close()
                await asyncio.sleep(2)

            except Exception as exc:
                logger.error(f"BizimToptan page error for {url}: {repr(exc)}")

        await browser.close()

    logger.info(f"BizimToptan scrape complete: {len(products)} total products")
    return products


# ─────────────────────────────────────────────────────────────────────────────
# carrefoursa.com — Direct Playwright scrape (no AI parsing needed)
# Confirmed selectors (inspect_carrefoursa.py):
#   cards:  [class*='product-card']   (30 per page)
#   name:   h3
#   price:  [class*='discounted']     e.g. '39,90 TL'
#   link:   a[class*='product']       relative href
# Pagination: Next button click loop (href='#', not URL params)
# ─────────────────────────────────────────────────────────────────────────────

async def scrape_carrefoursa_direct() -> list:
    """
    Scrape all CARREFOURSA_TARGET_URLS using Playwright.
    Each category page paginates via a Next button (JS, not URL params).
    Returns list of dicts: product_name, current_price, market_name, product_url.
    """
    products = []
    BASE_URL = "https://www.carrefoursa.com"

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

        for url in CARREFOURSA_TARGET_URLS:
            page_num = 1
            url_total = 0
            try:
                logger.info(f"CarrefourSA: scraping {url}")
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=60_000)
                await asyncio.sleep(2)

                while True:
                    cards = await page.query_selector_all("[class*='product-card']")
                    if not cards:
                        logger.warning(f"CarrefourSA: no cards on page {page_num} of {url}")
                        break

                    page_count = 0
                    for card in cards:
                        try:
                            name_el  = await card.query_selector("h3")
                            price_el = await card.query_selector("[class*='discounted']")
                            link_el  = await card.query_selector("a[class*='product']")

                            if not name_el or not price_el:
                                continue

                            name = (await name_el.inner_text()).strip()
                            price_raw = (await price_el.inner_text()).strip()
                            price = _parse_tr_price(price_raw)

                            if not name or price <= 0:
                                continue

                            href = await link_el.get_attribute("href") if link_el else ""
                            if href and not href.startswith("http"):
                                product_url = BASE_URL + "/" + href.lstrip("/")
                            else:
                                product_url = href or url

                            products.append({
                                "product_name": name,
                                "current_price": price,
                                "market_name": "CarrefourSA",
                                "product_url": product_url,
                            })
                            page_count += 1

                        except Exception as card_exc:
                            logger.debug(f"CarrefourSA card error: {card_exc}")

                    url_total += page_count
                    logger.info(
                        f"CarrefourSA: page {page_num} of {url} -> {page_count} products"
                    )

                    # Click Next button; stop if absent or disabled
                    next_btn = await page.query_selector(
                        "[class*='pager'] a.next, [class*='pager'] a[class*='next'], "
                        "[class*='pager'] li.next > a, [class*='next-page'] a"
                    )
                    if not next_btn:
                        break
                    is_disabled = await next_btn.get_attribute("class") or ""
                    if "disabled" in is_disabled.lower():
                        break

                    await next_btn.click()
                    await asyncio.sleep(2)
                    page_num += 1

                    # Safety cap: max 20 pages per category
                    if page_num > 20:
                        logger.warning(f"CarrefourSA: hit 20-page cap for {url}")
                        break

                logger.info(f"CarrefourSA: {url_total} products total from {url}")
                await page.close()
                await asyncio.sleep(1)

            except Exception as exc:
                logger.error(f"CarrefourSA page error for {url}: {repr(exc)}")

        await browser.close()

    logger.info(f"CarrefourSA scrape complete: {len(products)} total products")
    return products


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
