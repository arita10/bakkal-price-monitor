"""
src/parsers/scrapers.py — All shop scraper configs in one place.

Each shop is defined as a ShopConfig. The common scrape_shop() engine
handles browser, pagination, and product extraction.

Add a new shop by adding a new ShopConfig + scrape_<name>() function.
"""

import asyncio
import logging
import re

from src.parsers.base import ShopConfig, scrape_shop, URL_PARAM, NEXT_BUTTON, NONE
from src.utils import parse_tr_price

logger = logging.getLogger("bakkal_monitor.parsers")


# ── BizimToptan ───────────────────────────────────────────────────────────────

async def _bizimtoptan_wait(page) -> None:
    """Wait for jQuery tmpl to resolve placeholder text."""
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
        logger.warning("BizimToptan: tmpl render timeout, trying anyway")
    await asyncio.sleep(2)


BIZIMTOPTAN = ShopConfig(
    market_name    = "Bizim Toptan",
    base_url       = "https://www.bizimtoptan.com.tr",
    target_urls    = [
        "https://www.bizimtoptan.com.tr/kampanyalar",
        "https://www.bizimtoptan.com.tr/indirimli-urunler",
    ],
    card_sel            = ".product-box-container",
    name_sel            = ".productbox-name",
    price_sel           = ".campaign-price",
    fallback_price_sel  = ".product-price",
    link_sel            = "a.product-item",
    pagination          = NONE,
    pre_scrape_hook     = _bizimtoptan_wait,
)


# ── CarrefourSA ───────────────────────────────────────────────────────────────

CARREFOURSA = ShopConfig(
    market_name  = "CarrefourSA",
    base_url     = "https://www.carrefoursa.com",
    target_urls  = [
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
    ],
    card_sel     = "[class*='product-card']",
    name_sel     = "h3",
    price_sel    = "[class*='discounted']",
    link_sel     = "a[class*='product']",
    pagination   = NEXT_BUTTON,
    next_btn_sel = "[class*='pager'] a.next, [class*='pager'] a[class*='next'], "
                   "[class*='pager'] li.next > a, [class*='next-page'] a",
    max_pages    = 20,
)


# ── Migros ────────────────────────────────────────────────────────────────────

async def _migros_wait(page) -> None:
    """Angular: wait for sm-list-page-item components to render."""
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('sm-list-page-item').length > 0",
            timeout=15_000,
        )
    except Exception:
        logger.warning("Migros: render timeout, trying anyway")
    await asyncio.sleep(2)


MIGROS = ShopConfig(
    market_name  = "Migros",
    base_url     = "https://www.migros.com.tr",
    target_urls  = [
        "https://www.migros.com.tr/tum-indirimli-urunler-dt-0",
        "https://www.migros.com.tr/beslenme-yasam-tarzi-ptt-1",
        "https://www.migros.com.tr/sadece-migrosta-ptt-2",
        "https://www.migros.com.tr/migroskop-urunleri-dt-3",
        "https://www.migros.com.tr/ramazan-c-1209a",
        "https://www.migros.com.tr/meyve-sebze-c-2",
        "https://www.migros.com.tr/et-tavuk-balik-c-3",
        "https://www.migros.com.tr/sut-kahvaltilik-c-4",
        "https://www.migros.com.tr/temel-gida-c-5",
        "https://www.migros.com.tr/icecek-c-6",
        "https://www.migros.com.tr/reis-c-1222a",
        "https://www.migros.com.tr/atistirmalik-c-113fb",
        "https://www.migros.com.tr/dondurma-c-41b",
        "https://www.migros.com.tr/firin-pastane-c-7e",
        "https://www.migros.com.tr/bizim-yag-c-12155",
        "https://www.migros.com.tr/hazir-yemek-donuk-c-7d",
        "https://www.migros.com.tr/gurmepack-yemek-c-121f5",
        "https://www.migros.com.tr/deterjan-temizlik-c-7",
        "https://www.migros.com.tr/kisisel-bakim-kozmetik-saglik-c-8",
        "https://www.migros.com.tr/kagit-islak-mendil-c-8d",
        "https://www.migros.com.tr/bebek-c-9",
        "https://www.migros.com.tr/ev-yasam-c-a",
        "https://www.migros.com.tr/kitap-kirtasiye-oyuncak-c-118ec",
        "https://www.migros.com.tr/evcil-hayvan-c-a0",
    ],
    card_sel            = "sm-list-page-item",
    name_sel            = "[class*='product-name']",
    price_sel           = "[class*='sale-price']",
    fallback_price_sel  = "[class*='price']",
    link_sel            = "a[class*='product']",
    pagination          = URL_PARAM,
    page_param          = "sayfa",
    max_pages           = 20,
    pre_scrape_hook     = _migros_wait,
)


# ── SOK Market ────────────────────────────────────────────────────────────────

SOK = ShopConfig(
    market_name     = "SOK Market",
    base_url        = "https://www.sokmarket.com.tr",
    target_urls     = [
        "https://www.sokmarket.com.tr/win-kazandiran-urunler-pgrp-f353cf31-f728-425e-a453-5774219a76b8",
        "https://www.sokmarket.com.tr/haftanin-firsatlari-market-sgrp-146401",
        "https://www.sokmarket.com.tr/50-tl-ve-uzeri-indirimli-urunler-pgrp-11d42a6b-df28-4fe6-b1a3-7ad6b8d7f9a0",
        "https://www.sokmarket.com.tr/glutensiz-urunler-sgrp-172676",
        "https://www.sokmarket.com.tr/yemeklik-malzemeler-c-1770",
        "https://www.sokmarket.com.tr/et-ve-tavuk-ve-sarkuteri-c-160",
        "https://www.sokmarket.com.tr/meyve-ve-sebze-c-20",
        "https://www.sokmarket.com.tr/sut-ve-sut-urunleri-c-460",
        "https://www.sokmarket.com.tr/kahvaltilik-c-890",
        "https://www.sokmarket.com.tr/atistirmaliklar-c-20376",
        "https://www.sokmarket.com.tr/icecek-c-20505",
        "https://www.sokmarket.com.tr/ekmek-ve-pastane-c-1250",
        "https://www.sokmarket.com.tr/dondurulmus-urunler-c-1550",
        "https://www.sokmarket.com.tr/dondurma-c-31102",
        "https://www.sokmarket.com.tr/temizlik-c-20647",
        "https://www.sokmarket.com.tr/kagit-urunler-c-20875",
        "https://www.sokmarket.com.tr/kisisel-bakim-ve-kozmetik-c-20395",
        "https://www.sokmarket.com.tr/anne-bebek-ve-cocuk-c-20634",
        "https://www.sokmarket.com.tr/evcil-dostlar-c-20880",
    ],
    card_sel        = "[class*='ProductCard']",
    name_sel        = "h2",
    price_sel       = "span[class*='price']",
    link_via_parent = True,   # link is on the parent <a>, not inside card
    pagination      = URL_PARAM,
    page_param      = "page",
    max_pages       = 10,
)


# ── A101 Kapıda ───────────────────────────────────────────────────────────────

def _a101_price(card_text: str) -> float:
    """Last ₺X,XX value in the card text = unit price."""
    text = card_text.replace("\n", " ")
    matches = re.findall(r"₺\s*(\d[\d.]*,\d+)", text)
    if matches:
        raw = matches[-1].replace(".", "").replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            pass
    return 0.0


async def _a101_extract_name(card) -> str:
    """Name comes from img[alt] inside the product link."""
    link_el = await card.query_selector("a[href*='kapida']")
    if link_el:
        imgs = await link_el.query_selector_all("img[alt]")
        for img in imgs:
            alt = (await img.get_attribute("alt") or "").strip()
            if alt and not any(
                slug in alt.lower()
                for slug in ["cok-al", "haftanin", "indirimli", "aldin", "bizim", "doritos"]
            ):
                return alt
    # Fallback: last non-price line in card text
    card_text = (await card.inner_text()).strip()
    lines = [l.strip() for l in card_text.split("\n") if l.strip()]
    for line in reversed(lines):
        if not re.match(r"^[₺\d\s,AL]+$", line) and len(line) > 3:
            return line
    return ""


async def _a101_extract_price(card) -> float:
    card_text = (await card.inner_text()).strip()
    return _a101_price(card_text)


async def _a101_wait(page) -> None:
    try:
        await page.wait_for_function(
            "() => document.querySelectorAll('[data-product-id]').length > 0",
            timeout=15_000,
        )
    except Exception:
        logger.warning("A101 Kapida: render timeout, trying anyway")
    await asyncio.sleep(2)


A101KAPIDA = ShopConfig(
    market_name     = "A101 Kapida",
    base_url        = "https://www.a101.com.tr",
    target_urls     = [
        "https://www.a101.com.tr/kapida/doritos-urunleri-S4289",
        "https://www.a101.com.tr/kapida/bizim-yag-S1983",
        "https://www.a101.com.tr/kapida/haftanin-yildizlari",
        "https://www.a101.com.tr/kapida/10tl-ve-uzeri-alisverislerinizde-indirimli-urunler",
        "https://www.a101.com.tr/kapida/cok-al-az-ode",
        "https://www.a101.com.tr/kapida/aldin-aldin",
    ],
    card_sel        = "[data-product-id]",
    name_sel        = "",           # overridden by extract_name
    price_sel       = "",           # overridden by extract_price
    link_sel        = "a[href*='kapida']",
    pagination      = NONE,
    cookie_sel      = "button:has-text('Kabul Et')",
    pre_scrape_hook = _a101_wait,
    extract_name    = _a101_extract_name,
    extract_price   = _a101_extract_price,
)


# ── Public scrape functions (keep same interface as before) ───────────────────

async def scrape_bizimtoptan() -> list[dict]:
    return await scrape_shop(BIZIMTOPTAN)

async def scrape_carrefoursa() -> list[dict]:
    return await scrape_shop(CARREFOURSA)

async def scrape_migros() -> list[dict]:
    return await scrape_shop(MIGROS)

async def scrape_sok() -> list[dict]:
    return await scrape_shop(SOK)

async def scrape_a101kapida() -> list[dict]:
    return await scrape_shop(A101KAPIDA)
