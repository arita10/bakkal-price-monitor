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
import time

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

def search_prices(supabase, query: str) -> list[dict]:
    """
    Search price_history for products whose name contains the query string.
    Returns the most recent record per product_url.
    Uses ilike for case-insensitive Turkish-friendly matching.
    """
    try:
        response = (
            supabase.table("price_history")
            .select(
                "product_name, market_name, current_price, "
                "previous_price, price_drop_pct, scraped_date, product_url"
            )
            .ilike("product_name", f"%{query}%")
            .order("scraped_at", desc=True)
            .limit(50)
            .execute()
        )
        rows = response.data or []

        # Deduplicate: keep only the freshest record per product_url
        seen: set[str] = set()
        unique: list[dict] = []
        for row in rows:
            key = row["product_url"]
            if key not in seen:
                seen.add(key)
                unique.append(row)

        return unique

    except Exception as exc:
        logger.error(f"search_prices error for '{query}': {exc}")
        return []


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


def build_price_reply(query: str, rows: list[dict]) -> str:
    """
    Build a Telegram message showing prices grouped by market.
    """
    if not rows:
        return (
            f"❌ *'{query}'* için fiyat bulunamadı.\n\n"
            f"Farklı bir kelime deneyin (örn: süt, ekmek, yağ, şeker)"
        )

    # Group by market
    by_market: dict[str, list[dict]] = {}
    for row in rows:
        m = row["market_name"]
        by_market.setdefault(m, []).append(row)

    lines = [f"🛒 *{query.upper()}* fiyatları:\n"]

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
        rows = search_prices(supabase, query)
        send(token, chat_id, build_price_reply(query, rows))

    elif lower.startswith("/"):
        # Unknown slash command — ignore silently
        pass

    else:
        # Treat any plain text as a product query
        rows = search_prices(supabase, text)
        send(token, chat_id, build_price_reply(text, rows))


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


if __name__ == "__main__":
    run_bot()
