"""
bot.py — Interactive Telegram Bot for price queries.

Users send a product name (e.g. "süt", "ekmek", "yağ") and the bot
replies with a price comparison table from the latest Supabase data.

Run locally:   python bot.py
Run in CI:     add a separate GitHub Actions workflow or run as a service.

Commands:
  /start        — Welcome message
  /help         — Usage instructions
  /fiyat <ürün> — Query prices for a product (also works without /fiyat)
  /markets      — List all markets in the database
  /son          — Show the 10 most recently scraped products
"""

import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from supabase import create_client

from config import load_config

logger = logging.getLogger("bakkal_bot")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# How many seconds to wait between Telegram long-poll requests
POLL_TIMEOUT = 30
# Max results returned per market in a price query
MAX_RESULTS_PER_MARKET = 3

# ─────────────────────────────────────────────────────────────────────────────
# Smart query expansion: English → Turkish + fuzzy Turkish char variants
# ─────────────────────────────────────────────────────────────────────────────

# English keyword → Turkish search terms (list of alternatives to try)
EN_TR_MAP: dict[str, list[str]] = {
    # Dairy
    "milk": ["süt"],
    "yogurt": ["yoğurt", "yogurt"],
    "yoghurt": ["yoğurt", "yogurt"],
    "cheese": ["peynir"],
    "butter": ["tereyağ", "tereyağı"],
    "cream": ["krema", "kaymak"],
    "egg": ["yumurta"],
    "eggs": ["yumurta"],
    # Bread & grain
    "bread": ["ekmek"],
    "flour": ["un"],
    "rice": ["pirinç", "pilav"],
    "pasta": ["makarna"],
    "noodle": ["makarna", "erişte"],
    "noodles": ["makarna", "erişte"],
    # Oils & fats
    "oil": ["yağ"],
    "sunflower oil": ["ayçiçek yağı"],
    "olive oil": ["zeytinyağı"],
    "margarine": ["margarin"],
    # Sugar & sweets
    "sugar": ["şeker"],
    "honey": ["bal"],
    "jam": ["reçel"],
    "chocolate": ["çikolata"],
    # Beverages
    "tea": ["çay"],
    "coffee": ["kahve"],
    "water": ["su", "içme suyu"],
    "juice": ["meyve suyu", "meyve"],
    "cola": ["kola", "cola"],
    # Meat & protein
    "chicken": ["tavuk", "piliç"],
    "beef": ["et", "dana"],
    "meat": ["et"],
    "fish": ["balık"],
    "tuna": ["ton balığı", "ton"],
    # Vegetables & fruit
    "tomato": ["domates"],
    "potato": ["patates"],
    "onion": ["soğan"],
    "garlic": ["sarımsak"],
    "pepper": ["biber"],
    "apple": ["elma"],
    "banana": ["muz"],
    "orange": ["portakal"],
    "lemon": ["limon"],
    # Condiments & other
    "salt": ["tuz"],
    "vinegar": ["sirke"],
    "ketchup": ["ketçap"],
    "mayonnaise": ["mayonez"],
    "mustard": ["hardal"],
    "soap": ["sabun"],
    "detergent": ["deterjan"],
    "shampoo": ["şampuan"],
    "napkin": ["peçete"],
    "paper towel": ["kağıt havlu"],
    "toilet paper": ["tuvalet kağıdı"],
}

# Turkish character substitutions for fuzzy expansion
# When a user types without special chars, generate variants
_TR_FUZZY: list[tuple[str, str]] = [
    ("s", "ş"),
    ("c", "ç"),
    ("g", "ğ"),
    ("i", "ı"),
    ("o", "ö"),
    ("u", "ü"),
]


def expand_query(raw: str) -> list[str]:
    """
    Given a raw user query, return an ordered list of search terms to try.

    Strategy:
    1. If the raw term is an English keyword, translate to Turkish term(s).
    2. Always include the original query.
    3. Generate Turkish-char variants (e.g. "sut" → "süt", "sis" → "şiş").
    Only unique terms are returned, in priority order.
    """
    term = raw.strip().lower()
    candidates: list[str] = []

    # 1. English translation
    if term in EN_TR_MAP:
        candidates.extend(EN_TR_MAP[term])

    # Also check multi-word partial matches (e.g. "sunflower" in "sunflower oil")
    for en_key, tr_vals in EN_TR_MAP.items():
        if term in en_key or en_key in term:
            for v in tr_vals:
                if v not in candidates:
                    candidates.append(v)

    # 2. Original query
    if raw.strip() not in candidates:
        candidates.append(raw.strip())

    # 3. Generate Turkish character variants for short terms (≤ 6 chars)
    if len(term) <= 6:
        for ascii_ch, tr_ch in _TR_FUZZY:
            if ascii_ch in term:
                variant = term.replace(ascii_ch, tr_ch)
                if variant not in candidates:
                    candidates.append(variant)

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for c in candidates:
        if c.lower() not in seen:
            seen.add(c.lower())
            unique.append(c)

    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────────────────────────────────────

def tg(token: str, method: str, **kwargs) -> dict:
    """Call a Telegram Bot API method. Returns the JSON response."""
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        resp = requests.post(url, json=kwargs, timeout=35)
        return resp.json()
    except Exception as exc:
        logger.error(f"Telegram API error [{method}]: {exc}")
        return {}


def send(token: str, chat_id: int, text: str) -> None:
    """Send a Markdown-formatted message."""
    tg(token, "sendMessage",
       chat_id=chat_id,
       text=text,
       parse_mode="Markdown",
       disable_web_page_preview=True)


def get_updates(token: str, offset: int) -> list[dict]:
    """Long-poll for new updates. Returns list of update objects."""
    data = tg(token, "getUpdates", offset=offset, timeout=POLL_TIMEOUT)
    return data.get("result", [])


# ─────────────────────────────────────────────────────────────────────────────
# Supabase queries
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_by_term(supabase, term: str) -> list[dict]:
    """Single ilike query against price_history, deduplicated by product_url."""
    response = (
        supabase.table("price_history")
        .select(
            "product_name, market_name, current_price, "
            "previous_price, price_drop_pct, scraped_date, product_url"
        )
        .ilike("product_name", f"%{term}%")
        .order("scraped_at", desc=True)
        .limit(50)
        .execute()
    )
    rows = response.data or []
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        key = row["product_url"]
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def search_prices(supabase, query: str) -> tuple[list[dict], str]:
    """
    Search price_history using smart query expansion.
    Tries multiple terms (English translation, original, Turkish char variants)
    and returns results from the first term that yields results.

    Returns (rows, matched_term) — matched_term is what actually found results.
    """
    terms = expand_query(query)
    logger.info(f"Query '{query}' expanded to: {terms}")

    for term in terms:
        try:
            rows = _fetch_by_term(supabase, term)
            if rows:
                logger.info(f"Found {len(rows)} results for term '{term}'")
                return rows, term
        except Exception as exc:
            logger.error(f"search_prices error for '{term}': {exc}")

    return [], query


def get_all_markets(supabase) -> list[str]:
    """Return sorted list of distinct market names in the database."""
    try:
        response = (
            supabase.table("price_history")
            .select("market_name")
            .execute()
        )
        markets = sorted({r["market_name"] for r in (response.data or [])})
        return markets
    except Exception as exc:
        logger.error(f"get_all_markets error: {exc}")
        return []


def get_recent_products(supabase, limit: int = 10) -> list[dict]:
    """Return the most recently scraped unique products."""
    try:
        response = (
            supabase.table("price_history")
            .select("product_name, market_name, current_price, scraped_date")
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception as exc:
        logger.error(f"get_recent_products error: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Response formatters
# ─────────────────────────────────────────────────────────────────────────────

def fmt_price(price) -> str:
    """Format float as Turkish price string: 1249.99 → '1.249,99'"""
    try:
        f = float(price)
        # Format with 2 decimal places, then swap separators
        s = f"{f:,.2f}"          # e.g. "1,249.99"
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return s
    except Exception:
        return str(price)


def build_price_reply(query: str, rows: list[dict], matched_term: str = "") -> str:
    """
    Build a Telegram message showing prices grouped by market.
    If matched_term differs from query (translation/fuzzy), show a note.
    """
    if not rows:
        return (
            f"❌ *'{query}'* için fiyat bulunamadı.\n\n"
            f"Farklı bir kelime deneyin (örn: süt, ekmek, yağ, şeker)"
        )

    # Header — note if we matched via a translated/expanded term
    display = matched_term if matched_term else query
    if matched_term and matched_term.lower() != query.lower():
        header = f"🛒 *{display.upper()}* fiyatları: _('{query}' için)_\n"
    else:
        header = f"🛒 *{display.upper()}* fiyatları:\n"

    # Group by market
    by_market: dict[str, list[dict]] = {}
    for row in rows:
        m = row["market_name"]
        by_market.setdefault(m, []).append(row)

    lines = [header]

    for market in sorted(by_market.keys()):
        items = by_market[market][:MAX_RESULTS_PER_MARKET]
        lines.append(f"🏪 *{market}*")
        for item in items:
            price_str = fmt_price(item["current_price"])
            name = item["product_name"]
            drop = item.get("price_drop_pct")
            drop_str = ""
            if drop is not None and float(drop) > 0:
                drop_str = f" 📉 -%{fmt_price(drop)}"
            elif drop is not None and float(drop) < 0:
                drop_str = f" 📈 +%{fmt_price(abs(float(drop)))}"
            lines.append(f"  • {name} — *{price_str} TL*{drop_str}")
        lines.append("")

    date = rows[0].get("scraped_date", "")
    lines.append(f"_📅 Son güncelleme: {date}_")

    return "\n".join(lines)


def build_markets_reply(markets: list[str]) -> str:
    if not markets:
        return "❌ Veritabanında henüz market verisi yok."
    lines = ["🏪 *Takip Edilen Marketler:*\n"]
    for m in markets:
        lines.append(f"  • {m}")
    return "\n".join(lines)


def build_recent_reply(rows: list[dict]) -> str:
    if not rows:
        return "❌ Henüz veri yok."
    lines = ["🕒 *Son Güncellenen Ürünler:*\n"]
    for row in rows:
        price_str = fmt_price(row["current_price"])
        lines.append(
            f"  • {row['product_name']} — *{price_str} TL* [{row['market_name']}]"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Message handler
# ─────────────────────────────────────────────────────────────────────────────

def handle_message(token: str, supabase, chat_id: int, text: str) -> None:
    """Route an incoming message to the right handler."""
    text = text.strip()
    lower = text.lower()

    if lower in ("/start", "/start@bakkalbot"):
        send(token, chat_id,
             "👋 *Bakkal Fiyat Botu'na hoşgeldiniz!*\n\n"
             "Bir ürün adı yazın, en güncel fiyatları getireyim.\n\n"
             "Örnekler: `süt`, `ekmek`, `yağ`, `şeker`, `çay`\n\n"
             "/help — Tüm komutlar\n"
             "/markets — Takip edilen marketler\n"
             "/son — Son güncellenen ürünler")

    elif lower in ("/help", "/help@bakkalbot"):
        send(token, chat_id,
             "*📖 Kullanım:*\n\n"
             "Herhangi bir ürün adı yazın:\n"
             "`süt` → tüm marketlerdeki süt fiyatları\n"
             "`200ml süt` → daha spesifik arama\n\n"
             "*Komutlar:*\n"
             "/fiyat <ürün> — Fiyat sorgula\n"
             "/markets — Takip edilen marketler\n"
             "/son — Son güncellenen 10 ürün\n"
             "/help — Bu yardım mesajı")

    elif lower in ("/markets", "/markets@bakkalbot"):
        markets = get_all_markets(supabase)
        send(token, chat_id, build_markets_reply(markets))

    elif lower in ("/son", "/son@bakkalbot"):
        rows = get_recent_products(supabase)
        send(token, chat_id, build_recent_reply(rows))

    elif lower.startswith("/fiyat "):
        query = text[7:].strip()
        if not query:
            send(token, chat_id, "❓ Kullanım: `/fiyat süt`")
            return
        rows, matched = search_prices(supabase, query)
        send(token, chat_id, build_price_reply(query, rows, matched))

    elif lower.startswith("/"):
        # Unknown slash command — ignore silently
        pass

    else:
        # Treat any plain text as a product query
        rows, matched = search_prices(supabase, text)
        send(token, chat_id, build_price_reply(text, rows, matched))


# ─────────────────────────────────────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────────────────────────────────────

def run_bot() -> None:
    config = load_config()
    token = config["TELEGRAM_BOT_TOKEN"]
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])

    logger.info("=== Bakkal Price Bot starting (long-poll mode) ===")

    # Get the bot's own username for logging
    me = tg(token, "getMe")
    bot_name = me.get("result", {}).get("username", "unknown")
    logger.info(f"Bot running as @{bot_name}")

    offset = 0
    while True:
        try:
            updates = get_updates(token, offset)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as exc:
            logger.error(f"Polling error: {exc}")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message")
            if not message:
                continue

            chat_id = message["chat"]["id"]
            text = message.get("text", "").strip()
            if not text:
                continue

            user = message.get("from", {})
            username = user.get("username") or user.get("first_name", "?")
            logger.info(f"Message from @{username} ({chat_id}): {text!r}")

            try:
                handle_message(token, supabase, chat_id, text)
            except Exception as exc:
                logger.error(f"handle_message error: {exc}")
                try:
                    send(token, chat_id, "❌ Bir hata oluştu, lütfen tekrar deneyin.")
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Minimal HTTP server — required by Render Web Service to bind a port
# ─────────────────────────────────────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass  # Suppress HTTP access logs


def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info(f"Health server listening on port {port}")
    server.serve_forever()


def self_ping():
    """Ping own health endpoint every 10 minutes to prevent Render free tier sleep."""
    port = int(os.environ.get("PORT", 10000))
    url = f"http://0.0.0.0:{port}/"
    while True:
        time.sleep(600)  # 10 minutes
        try:
            requests.get(url, timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # Start health check HTTP server
    t1 = threading.Thread(target=start_health_server, daemon=True)
    t1.start()
    # Self-ping to prevent Render free tier sleep
    t2 = threading.Thread(target=self_ping, daemon=True)
    t2.start()
    # Run Telegram bot on main thread
    run_bot()
