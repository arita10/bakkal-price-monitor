"""
src/agents/marketfiyati_api.py — marketfiyati.org.tr REST API client.

Queries the public price API for Turkish grocery keywords and returns
structured dicts directly — no AI parsing needed.
"""

import asyncio
import logging

import requests

logger = logging.getLogger("bakkal_monitor.agents.marketfiyati_api")

MARKETFIYATI_API_URL = "https://api.marketfiyati.org.tr/api/v2/search"

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
    "streç film", "çöp torbası", "ampul", "yara bandı", "traş köpüğü",
]

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
    return _MARKET_NAME_MAP.get(raw.lower().strip(), raw.strip().title())


def fetch_keyword(keyword: str, lat: float, lon: float) -> list[dict]:
    """
    Query the API for a single keyword.
    Returns list of dicts: product_name, current_price, market_name, product_url.
    Returns [] on any error.
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

        items = data if isinstance(data, list) else data.get(
            "productDepotInfoList", data.get("data", [])
        )
        if not isinstance(items, list):
            items = []

        products = []
        for item in items:
            try:
                name = (item.get("title") or item.get("name") or "").strip()
                price_val = item.get("price") or item.get("currentPrice") or 0
                market_raw = (
                    item.get("marketAdi") or item.get("market") or item.get("depotName") or ""
                )
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

        logger.info(f"marketfiyati API: '{keyword}' -> {len(products)} product(s)")
        return products

    except requests.RequestException as exc:
        logger.error(f"marketfiyati API error for '{keyword}': {exc}")
        return []


async def fetch_all(config: dict) -> list[dict]:
    """
    Query the API for all MARKETFIYATI_KEYWORDS sequentially.
    Applies a 1.5 s delay between calls to respect rate limits.
    Returns structured dicts — no AI parsing needed.
    """
    all_products: list[dict] = []
    lat = config["SHOP_LAT"]
    lon = config["SHOP_LON"]
    total = len(MARKETFIYATI_KEYWORDS)

    logger.info(f"Querying marketfiyati API for {total} keywords near ({lat}, {lon})")

    for i, keyword in enumerate(MARKETFIYATI_KEYWORDS, start=1):
        items = fetch_keyword(keyword, lat, lon)
        all_products.extend(items)
        if i % 10 == 0 or i == total:
            logger.info(
                f"  marketfiyati progress: {i}/{total} keywords done, "
                f"{len(all_products)} products so far"
            )
        await asyncio.sleep(1.5)

    logger.info(f"marketfiyati API complete: {len(all_products)} product(s) total")
    return all_products
