"""
src/parsers/carrefoursa.py — CarrefourSA scraper (carrefoursa.com)

Confirmed selectors (inspect_carrefoursa.py):
  card:   [class*='product-card']   (30 per page)
  name:   h3
  price:  [class*='discounted']     e.g. '39,90 TL'
  link:   a[class*='product']       relative href

Pagination: Next button click loop (href='#', not URL params).
Max 20 pages per category (safety cap).
"""

import asyncio
import logging

from playwright.async_api import async_playwright

from src.browsers.playwright_browser import new_context
from src.utils import parse_tr_price

logger = logging.getLogger("bakkal_monitor.parsers.carrefoursa")

BASE_URL = "https://www.carrefoursa.com"
MAX_PAGES = 20

TARGET_URLS = [
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


async def scrape() -> list[dict]:
    """
    Scrape all CarrefourSA TARGET_URLS using Playwright.
    Each category paginates via a Next button (JS, not URL params).
    Returns list of dicts: product_name, current_price, market_name, product_url.
    """
    products = []

    async with async_playwright() as p:
        browser, context = await new_context(p)

        for url in TARGET_URLS:
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
                            price = parse_tr_price(price_raw)

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
                    logger.info(f"CarrefourSA: page {page_num} of {url} -> {page_count} products")

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

                    if page_num > MAX_PAGES:
                        logger.warning(f"CarrefourSA: hit {MAX_PAGES}-page cap for {url}")
                        break

                logger.info(f"CarrefourSA: {url_total} products total from {url}")
                await page.close()
                await asyncio.sleep(1)

            except Exception as exc:
                logger.error(f"CarrefourSA page error for {url}: {repr(exc)}")

        await browser.close()

    logger.info(f"CarrefourSA scrape complete: {len(products)} total products")
    return products
