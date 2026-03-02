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

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from openai import OpenAI
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
    # Frozen & snacks
    "ice cream": ["dondurma"],
    "frozen": ["dondurulmuş"],
    "biscuit": ["bisküvi"],
    "cookie": ["bisküvi", "kurabiye"],
    "chips": ["cips"],
    "cracker": ["kraker"],
    "cake": ["pasta", "kek"],
    # Condiments extended
    "jam": ["reçel"],
    "pickle": ["turşu"],
    "sauce": ["sos"],
    # Fruit extended
    "grape": ["üzüm"],
    "strawberry": ["çilek"],
    "watermelon": ["karpuz"],
    "melon": ["kavun"],
    "pear": ["armut"],
    # Household
    "laundry": ["çamaşır"],
    "bleach": ["çamaşır suyu"],
    "sponge": ["sünger"],
    "tissue": ["peçete", "mendil"],
    # Baby
    "diaper": ["bez"],
    "nappy": ["bez"],
    "wipe": ["ıslak mendil"],
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


import re as _re
import string as _string

# Words to strip when user writes a full sentence like "how about price of milk?"
_FILLER_WORDS = {
    # English
    "how", "about", "price", "of", "the", "a", "an", "what", "is", "are",
    "show", "me", "get", "find", "tell", "check", "much", "does", "cost",
    "for", "please", "pls", "can", "you", "i", "want", "need", "buy",
    "any", "do", "have", "give", "look", "search",
    # Turkish
    "fiyatı", "fiyat", "ne", "kadar", "nedir", "var", "mı", "mi", "mu", "mü",
    "hangi", "en", "ucuz", "pahalı", "bul", "göster", "ver", "lütfen",
    "acaba", "ürün", "ürünü", "almak", "istiyorum",
}


def _clean_query(raw: str) -> str:
    """
    Strip punctuation and filler words from a sentence to extract the product keyword.
    'How about price of 0.5 le water?' → 'water'
    'Su?' → 'Su'
    """
    # Remove punctuation except Turkish letters
    cleaned = _re.sub(r"[^\w\sğüşıöçĞÜŞİÖÇ]", " ", raw, flags=_re.UNICODE)
    # Split and filter filler words and pure numbers
    words = [
        w for w in cleaned.split()
        if w.lower() not in _FILLER_WORDS
        and not _re.match(r"^\d+([.,]\d+)?$", w)
        and len(w) >= 2
    ]
    if words:
        # Return the last meaningful word (usually the product name at end of sentence)
        return words[-1]
    return raw.strip()


def expand_query(raw: str) -> list[str]:
    """
    Given a raw user query, return an ordered list of search terms to try.

    Strategy:
    0. Clean punctuation and strip filler words from sentences.
    1. If the cleaned term matches an English keyword, translate to Turkish.
    2. Always include the cleaned query.
    3. Generate Turkish character variants for short terms (≤ 6 chars).
    Only unique terms are returned, in priority order.
    """
    cleaned = _clean_query(raw)
    term = cleaned.strip().lower()
    candidates: list[str] = []

    # 1. English translation — exact match
    if term in EN_TR_MAP:
        candidates.extend(EN_TR_MAP[term])

    # Partial match: e.g. "sunflower" matches key "sunflower oil"
    for en_key, tr_vals in EN_TR_MAP.items():
        if term in en_key or en_key in term:
            for v in tr_vals:
                if v not in candidates:
                    candidates.append(v)

    # 2. Cleaned query itself
    if cleaned not in candidates:
        candidates.append(cleaned)

    # 3. Turkish character variants for short terms
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

    logger.debug(f"expand_query: '{raw}' → cleaned='{cleaned}' → {unique}")
    return unique


# ─────────────────────────────────────────────────────────────────────────────
# Telegram helpers
# ─────────────────────────────────────────────────────────────────────────────

def tg(token: str, method: str, **kwargs) -> dict:
    """Call a Telegram Bot API method. Returns the JSON response.

    For getUpdates long-poll calls, timeout kwarg is the Telegram server-wait
    seconds. The HTTP request timeout must be larger — we add 10 s headroom.
    """
    url = TELEGRAM_API.format(token=token, method=method)
    # Use server-side timeout + 10 s as the HTTP socket timeout
    server_wait = kwargs.get("timeout", 0)
    http_timeout = max(30, server_wait + 10)
    try:
        resp = requests.post(url, json=kwargs, timeout=http_timeout)
        return resp.json()
    except Exception as exc:
        logger.error(f"Telegram API error [{method}]: {exc}")
        return {}


def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send(token: str, chat_id: int, text: str) -> None:
    """Send an HTML-formatted message. Logs API errors explicitly."""
    result = tg(token, "sendMessage",
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True)
    if result and not result.get("ok"):
        logger.error(f"sendMessage failed: {result.get('description')} | text[:80]={text[:80]!r}")


def get_updates(token: str, offset: int) -> list[dict]:
    """Long-poll for new updates. Returns list of update objects."""
    data = tg(token, "getUpdates", offset=offset, timeout=POLL_TIMEOUT)
    return data.get("result", [])


# ─────────────────────────────────────────────────────────────────────────────
# Supabase queries
# ─────────────────────────────────────────────────────────────────────────────

def _rank_relevance(product_name: str, term: str) -> int:
    """
    Score how well a product name matches the search term.
    Lower score = more relevant (sort ascending).

    0 — product name IS exactly the term (or starts with term + space)
    1 — term appears as a whole word inside the name
    2 — term appears at the start of the name (prefix)
    3 — term appears anywhere (substring fallback)
    """
    name_lower = product_name.lower().strip()
    term_lower = term.lower().strip()

    # Exact match or "term <something>" e.g. "süt 1L"
    if name_lower == term_lower or name_lower.startswith(term_lower + " "):
        return 0

    # Whole-word match: term surrounded by spaces or at boundaries
    import re as _re2
    if _re2.search(r'(?<!\w)' + _re2.escape(term_lower) + r'(?!\w)', name_lower):
        return 1

    # Prefix match: name starts with term
    if name_lower.startswith(term_lower):
        return 2

    # Substring fallback
    return 3


def _fetch_by_term(supabase, term: str) -> list[dict]:
    """
    Query price_history with ILIKE, then re-rank results so that
    whole-word matches (e.g. 'süt') appear before substring matches
    (e.g. 'bisküvi' matching 'küvi'). If any score-0/1 results exist,
    score-3 results are dropped entirely.
    """
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

    # Deduplicate by product_url
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        key = row["product_url"]
        if key not in seen:
            seen.add(key)
            row["_score"] = _rank_relevance(row["product_name"], term)
            unique.append(row)

    # Sort by relevance score
    unique.sort(key=lambda r: r["_score"])

    # If we have good matches (score 0 or 1), drop pure substring noise (score 3)
    best_score = unique[0]["_score"] if unique else 3
    if best_score <= 1:
        unique = [r for r in unique if r["_score"] <= 2]

    # Remove internal score key before returning
    for r in unique:
        r.pop("_score", None)

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


def get_best_deals(supabase, limit: int = 10) -> list[dict]:
    """Return today's biggest price drops using v_best_deals view."""
    try:
        response = (
            supabase.table("v_best_deals")
            .select("product_name, market_name, current_price, previous_price, price_drop_pct")
            .limit(limit)
            .execute()
        )
        return response.data or []
    except Exception:
        # Fallback: query price_history directly
        try:
            from datetime import date
            today = date.today().isoformat()
            response = (
                supabase.table("price_history")
                .select("product_name, market_name, current_price, previous_price, price_drop_pct")
                .eq("scraped_date", today)
                .gte("price_drop_pct", 5)
                .order("price_drop_pct", desc=True)
                .limit(limit)
                .execute()
            )
            return response.data or []
        except Exception as exc2:
            logger.error(f"get_best_deals error: {exc2}")
            return []


def get_price_history(supabase, product_url: str, days: int = 7) -> list[dict]:
    """Return last N days of price records for a product URL."""
    try:
        response = (
            supabase.table("price_history")
            .select("current_price, previous_price, price_drop_pct, scraped_date")
            .eq("product_url", product_url)
            .order("scraped_date", desc=True)
            .limit(days)
            .execute()
        )
        return response.data or []
    except Exception as exc:
        logger.error(f"get_price_history error: {exc}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# AI Chat — natural language questions answered from Supabase data
# ─────────────────────────────────────────────────────────────────────────────

_DB_SCHEMA = """
Table: price_history
Columns:
  product_name  TEXT       — full product name in Turkish
  market_name   TEXT       — retailer (BIM, A101, SOK, Migros, CarrefourSA, Hakmar, Tarim Kredi, Bizim Toptan)
  current_price NUMERIC    — price in Turkish Lira
  previous_price NUMERIC   — price on previous scrape (nullable)
  price_drop_pct NUMERIC   — % drop vs previous price (positive = cheaper, nullable)
  scraped_date  DATE       — date this price was recorded (YYYY-MM-DD)
  product_url   TEXT       — product page URL
"""

# English → Turkish product keyword map for the AI prompt
_EN_TR_HINT = """
English → Turkish product name translation (use Turkish in ILIKE):
  milk → süt               | bread → ekmek          | oil/sunflower oil → yağ
  egg/eggs → yumurta       | flour → un             | sugar → şeker
  rice → pirinç            | pasta/noodle → makarna | cheese → peynir
  butter → tereyağ         | tea → çay              | coffee → kahve
  water → su               | juice → meyve suyu     | chicken → tavuk
  meat/beef → et           | fish → balık           | tuna → ton
  tomato → domates         | potato → patates       | onion → soğan
  apple → elma             | banana → muz           | orange → portakal
  salt → tuz               | yogurt → yoğurt        | honey → bal
  olive oil → zeytinyağı   | chocolate → çikolata   | detergent → deterjan
  ice cream → dondurma     | frozen → dondurulmuş   | cake → pasta
  biscuit/cookie → bisküvi | chips/crisp → cips     | cracker → kraker
  jam → reçel              | pickle → turşu         | sauce → sos
  ketchup → ketçap         | mayonnaise → mayonez   | mustard → hardal
  vinegar → sirke          | pepper → biber         | garlic → sarımsak
  lemon → limon            | grape → üzüm           | strawberry → çilek
  watermelon → karpuz      | melon → kavun          | pear → armut
  shampoo → şampuan        | soap → sabun           | tissue → peçete
  toilet paper → tuvalet kağıdı | paper towel → kağıt havlu
  laundry → çamaşır       | bleach → çamaşır suyu  | sponge → sünger
  baby → bebek             | diaper/nappy → bez     | wipe → ıslak mendil
"""

_CHAT_SYSTEM = """You are Bakkal Asistan 🛒 — a warm, friendly grocery price helper for a small Turkish shop.
You help users find the best prices across Turkish supermarkets (BIM, A101, SOK, Migros, etc.).

=== PERSONALITY ===
- Warm, helpful, like a knowledgeable friend at the market
- Match the user's language: if they write in English → reply in English; Turkish → reply in Turkish
- Keep replies concise but friendly — add a short helpful tip when relevant
- Use light emojis naturally (🛒 💰 📉 ✅) but don't overdo it

=== TRANSLATION RULES (CRITICAL) ===
ALL product names in the database are in TURKISH. Always translate before searching.
""" + _EN_TR_HINT + """
For typos/missing Turkish chars: infer the product (e.g. "sut"→süt, "ekmk"→ekmek, "yogurt"→yoğurt).
Turkish chars: ş=s, ç=c, ğ=g, ı=i, ö=o, ü=u

=== SQL RULES ===
Database schema:
""" + _DB_SCHEMA + """
- Always SELECT from price_history table.
- Use ILIKE with Turkish keywords: product_name ILIKE '%süt%'
- For multiple translations use OR: (product_name ILIKE '%süt%' OR product_name ILIKE '%sut%')
- "cheapest/en ucuz": ORDER BY current_price ASC LIMIT 5
- "most expensive/en pahalı": ORDER BY current_price DESC LIMIT 5
- "deals/indirim/fırsat": WHERE price_drop_pct > 0 ORDER BY price_drop_pct DESC LIMIT 10
- market filter: market_name ILIKE '%bim%'
- "latest/güncel": ORDER BY scraped_date DESC LIMIT 10
- NEVER use DROP, INSERT, UPDATE, DELETE — SELECT only.
- Default LIMIT 10.

=== REPLY RULES ===
- Match user's language (English question → English answer, Turkish → Turkish)
- Format prices as Turkish style: 12.99 → "12,99 TL"
- Use bullet points for lists
- If results empty: kindly say the product wasn't found, mention what IS tracked
  (süt, ekmek, yağ, şeker, çay, makarna, pirinç, peynir, yumurta, tavuk, et),
  and suggest a similar product
- NEVER invent prices — only use real data from query results
- Do NOT use HTML tags — plain text only
- End with a short helpful tip or follow-up suggestion when it makes sense
"""

# Per-chat conversation history (last N turns) for context memory
# Key: chat_id (int), Value: list of {"role": ..., "content": ...}
_chat_history: dict[int, list[dict]] = {}
_HISTORY_MAX_TURNS = 6  # keep last 6 messages (3 user + 3 assistant)


def _strip_sql_fences(text: str) -> str:
    """Remove markdown code fences from GPT output."""
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        # parts[1] is the content between first pair of fences
        inner = parts[1] if len(parts) > 1 else text
        if inner.lower().startswith("sql"):
            inner = inner[3:]
        return inner.strip()
    return text


def chat_with_data(supabase, openai_client: OpenAI, user_question: str, chat_id: int = 0) -> str:
    """
    Use GPT-4o Mini to answer a natural language question about prices.

    - Translates English/typos to Turkish for SQL search
    - Keeps per-chat conversation history (last 6 messages) for follow-up questions
    - Matches reply language to the user's language
    - Returns plain text (no HTML tags)
    """
    # Copy history so mutations don't affect the stored list before we're done
    history = list(_chat_history.get(chat_id, []))

    try:
        # ── Step 1: Generate SQL with conversation context ───────────────────
        sql_messages = [
            {"role": "system", "content": _CHAT_SYSTEM},
            *history,
            {"role": "user", "content": (
                f"User question: {user_question}\n\n"
                "Translate any English product names to Turkish. "
                "Reply with ONLY the SQL SELECT query — no markdown, no explanation."
            )},
        ]

        sql_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=sql_messages,
            temperature=0.0,
            max_tokens=300,
        )
        sql_query = _strip_sql_fences(sql_response.choices[0].message.content)
        logger.info(f"AI chat SQL: {sql_query[:150]}")

        # Safety: only allow SELECT
        if not sql_query.upper().lstrip().startswith("SELECT"):
            logger.warning(f"AI returned non-SELECT query: {sql_query[:80]}")
            return "Üzgünüm, yalnızca veri okuma sorguları desteklenmektedir."

        # ── Step 2: Run query — RPC first, fallback to direct table query ────
        rows: list[dict] = []
        try:
            result = supabase.rpc("run_query", {"sql": sql_query}).execute()
            rows = result.data or []
            logger.info(f"AI chat RPC: {len(rows)} rows")
        except Exception as rpc_exc:
            logger.warning(f"RPC failed ({rpc_exc!r}), using fallback search")
            rows = _fallback_query(supabase, user_question)
            logger.info(f"AI chat fallback: {len(rows)} rows")

        # ── Step 3: Format friendly reply ────────────────────────────────────
        rows_text = json.dumps(rows[:10], ensure_ascii=False, default=str)

        reply_messages = [
            {"role": "system", "content": _CHAT_SYSTEM},
            *history,
            {"role": "user", "content": (
                f"User message: {user_question}\n\n"
                f"Database results:\n{rows_text}\n\n"
                "Reply naturally in the SAME language the user used. "
                "Be warm and friendly. Format prices as '12,99 TL'. "
                "Use bullet points for lists. No HTML tags."
            )},
        ]

        reply_response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=reply_messages,
            temperature=0.5,
            max_tokens=600,
        )
        reply = reply_response.choices[0].message.content.strip()

        # ── Step 4: Save to conversation history ─────────────────────────────
        if chat_id:
            history.append({"role": "user", "content": user_question})
            history.append({"role": "assistant", "content": reply})
            _chat_history[chat_id] = history[-_HISTORY_MAX_TURNS:]

        return reply

    except Exception as exc:
        logger.error(f"chat_with_data error ({type(exc).__name__}): {exc}", exc_info=True)
        # Return a specific message depending on where it likely failed
        err = str(exc).lower()
        if "openai" in err or "api" in err or "auth" in err or "key" in err:
            return "OpenAI API hatası oluştu. Lütfen daha sonra tekrar deneyin."
        if "supabase" in err or "postgrest" in err or "connection" in err:
            return "Veritabanı bağlantı hatası oluştu. Lütfen daha sonra tekrar deneyin."
        return "Bir hata oluştu, lütfen tekrar deneyin."


def _fallback_query(supabase, question: str) -> list[dict]:
    """
    Fallback when RPC is unavailable: uses the EN_TR_MAP already defined in the bot
    to translate English keywords, then runs an ilike search.
    """
    _skip = {"için", "nedir", "hangi", "kadar", "fiyat", "ürün", "what", "how",
             "much", "does", "cost", "price", "the", "is", "are", "show", "me"}
    words = [w for w in question.lower().split() if len(w) >= 2 and w not in _skip]
    if not words:
        return []

    # Try to translate English word first using EN_TR_MAP
    term = words[0]
    for en_key, tr_vals in EN_TR_MAP.items():
        if term in en_key or en_key in term:
            term = tr_vals[0]
            break

    try:
        response = (
            supabase.table("price_history")
            .select("product_name, market_name, current_price, previous_price, price_drop_pct, scraped_date")
            .ilike("product_name", f"%{term}%")
            .order("current_price", desc=False)
            .limit(10)
            .execute()
        )
        return response.data or []
    except Exception:
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


def build_price_reply(
    query: str,
    rows: list[dict],
    matched_term: str = "",
    history: list[dict] | None = None,
) -> str:
    """
    Build a friendly Telegram HTML message showing prices grouped by market.
    If `history` is provided (list of daily records), appends a mini trend
    for the first product found.
    """
    if not rows:
        return (
            f"😕 <b>'{_esc(query)}'</b> için şu an fiyat bulamadım.\n\n"
            f"Belki şunları deneyebilirsiniz:\n"
            f"<code>süt</code>  <code>ekmek</code>  <code>yağ</code>  <code>şeker</code>  <code>çay</code>\n\n"
            f"<i>İpucu: İngilizce de yazabilirsiniz — milk, bread, oil...</i>"
        )

    # Header — friendly note if we matched via a translated/expanded term
    display = matched_term if matched_term else query
    if matched_term and matched_term.lower() != query.lower():
        header = f"✅ '<b>{_esc(query)}</b>' için <b>{_esc(display.upper())}</b> sonuçlarını getirdim:\n"
    else:
        header = f"🛒 <b>{_esc(display.upper())}</b> fiyatları:\n"

    # Group by market
    by_market: dict[str, list[dict]] = {}
    for row in rows:
        m = row["market_name"]
        by_market.setdefault(m, []).append(row)

    lines = [header]

    for market in sorted(by_market.keys()):
        items = by_market[market][:MAX_RESULTS_PER_MARKET]
        lines.append(f"🏪 <b>{_esc(market)}</b>")
        for item in items:
            price_str = fmt_price(item["current_price"])
            name = _esc(item["product_name"])
            drop = item.get("price_drop_pct")
            drop_str = ""
            if drop is not None and float(drop) > 0:
                drop_str = f" 📉 -%{fmt_price(drop)}"
            elif drop is not None and float(drop) < 0:
                drop_str = f" 📈 +%{fmt_price(abs(float(drop)))}"
            lines.append(f"  • {name} — <b>{price_str} TL</b>{drop_str}")
        lines.append("")

    date = rows[0].get("scraped_date", "")
    lines.append(f"<i>📅 Son güncelleme: {_esc(str(date))}</i>")

    # Inline price trend (last 7 days) for the first result
    if history and len(history) >= 2:
        lines.append("\n📈 <b>Son 7 günlük fiyat geçmişi:</b>")
        for h in history[:7]:
            d = str(h["scraped_date"])
            p = fmt_price(h["current_price"])
            drop = h.get("price_drop_pct")
            if drop is not None and float(drop) > 0:
                arrow = " 📉"
            elif drop is not None and float(drop) < 0:
                arrow = " 📈"
            else:
                arrow = ""
            lines.append(f"  <code>{d}</code>  {p} TL{arrow}")

    # Contextual suggestions
    lines.append(_suggestion_line(display))

    return "\n".join(lines)


def build_markets_reply(markets: list[str]) -> str:
    if not markets:
        return (
            "🤔 Henüz market verisi yok.\n\n"
            "Veriler her sabah 07:00'de güncelleniyor, biraz sonra tekrar deneyin!"
        )
    lines = [f"🏪 <b>Takip ettiğim {len(markets)} market:</b>\n"]
    for m in markets:
        lines.append(f"  • {_esc(m)}")
    lines.append(
        "\n💡 <i>Bir ürün adı yazarak tüm marketlerde fiyat karşılaştırabilirsiniz.</i>\n"
        "Örnek: <code>süt</code>, <code>ekmek</code>, <code>yağ</code>"
    )
    return "\n".join(lines)


def build_recent_reply(rows: list[dict]) -> str:
    if not rows:
        return (
            "🤔 Henüz veri yok gibi görünüyor.\n\n"
            "Biraz sonra tekrar deneyin, veriler her sabah güncelleniyor! 🌅"
        )
    lines = ["🕒 <b>Az önce güncellenen ürünler:</b>\n"]
    for row in rows:
        price_str = fmt_price(row["current_price"])
        lines.append(
            f"  • {_esc(row['product_name'])} — <b>{price_str} TL</b> <i>{_esc(row['market_name'])}</i>"
        )
    lines.append(
        "\n💡 <i>Bir ürünü daha detaylı görmek için adını yazmanız yeterli!</i>\n"
        "Örnek: <code>süt</code>, <code>ekmek</code>, <code>yağ</code>"
    )
    return "\n".join(lines)


def build_deals_reply(rows: list[dict]) -> str:
    if not rows:
        return (
            "🤷 Bugün için kayıtlı fırsat bulunamadı.\n\n"
            "<i>Fırsatlar her sabah 07:00'de güncellenir. "
            "Veriler henüz yüklenmemiş olabilir.</i>"
        )
    lines = ["🔥 <b>Bugünün En İyi Fırsatları:</b>\n"]
    for row in rows:
        name = _esc(row["product_name"])
        market = _esc(row["market_name"])
        price = fmt_price(row["current_price"])
        prev  = fmt_price(row["previous_price"]) if row.get("previous_price") else "?"
        drop  = row.get("price_drop_pct", 0) or 0
        lines.append(
            f"📉 <b>{name}</b>\n"
            f"   {market} — <b>{price} TL</b>  <i>(eskiden {prev} TL, -%{fmt_price(drop)})</i>"
        )
    lines.append("\n<i>💡 Bir ürün adı yazarak daha fazla detay görebilirsiniz.</i>")
    return "\n".join(lines)


def build_history_reply(product_name: str, rows: list[dict]) -> str:
    """Show last N days of price for a product."""
    if not rows:
        return (
            f"📊 <b>{_esc(product_name)}</b> için henüz yeterli geçmiş veri yok.\n\n"
            "<i>Fiyat geçmişi her gün birikmektedir.</i>"
        )
    lines = [f"📊 <b>{_esc(product_name)}</b> — Son {len(rows)} günlük fiyat:\n"]
    for row in rows:
        date = str(row["scraped_date"])
        price = fmt_price(row["current_price"])
        drop = row.get("price_drop_pct")
        if drop is not None and float(drop) > 0:
            trend = f" 📉 -%{fmt_price(drop)}"
        elif drop is not None and float(drop) < 0:
            trend = f" 📈 +%{fmt_price(abs(float(drop)))}"
        else:
            trend = ""
        lines.append(f"  <code>{date}</code>  <b>{price} TL</b>{trend}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Suggestion chips — shown after every price reply
# ─────────────────────────────────────────────────────────────────────────────

# Curated "you might also want to check" suggestions per category
_SUGGESTIONS: dict[str, list[str]] = {
    "süt":       ["yoğurt", "peynir", "tereyağ"],
    "ekmek":     ["un", "makarna", "pirinç"],
    "yağ":       ["zeytinyağı", "tereyağ", "margarin"],
    "şeker":     ["çay", "kahve", "bal"],
    "çay":       ["kahve", "şeker", "su"],
    "kahve":     ["çay", "şeker", "süt"],
    "makarna":   ["pirinç", "un", "domates"],
    "pirinç":    ["makarna", "un", "yağ"],
    "peynir":    ["süt", "yumurta", "tereyağ"],
    "yumurta":   ["peynir", "süt", "tereyağ"],
    "tavuk":     ["et", "balık", "yumurta"],
    "et":        ["tavuk", "balık", "yumurta"],
    "domates":   ["biber", "soğan", "sarımsak"],
    "patates":   ["soğan", "domates", "yağ"],
    "elma":      ["muz", "portakal", "limon"],
}

_DEFAULT_SUGGESTIONS = ["süt", "ekmek", "yağ", "şeker", "çay"]


def _get_suggestions(matched_term: str) -> list[str]:
    """Return 3 related product suggestions for the matched term."""
    term = matched_term.lower()
    for key, sugs in _SUGGESTIONS.items():
        if key in term or term in key:
            return sugs[:3]
    return _DEFAULT_SUGGESTIONS[:3]


def _suggestion_line(matched_term: str) -> str:
    sugs = _get_suggestions(matched_term)
    chips = "  ".join(f"<code>{_esc(s)}</code>" for s in sugs)
    return f"\n💡 <b>Bunları da sorabilirsiniz:</b>\n{chips}"


# ─────────────────────────────────────────────────────────────────────────────
# Message handler
# ─────────────────────────────────────────────────────────────────────────────

def handle_message(token: str, supabase, openai_client: OpenAI, chat_id: int, text: str) -> None:
    """Route an incoming message to the right handler."""
    text = text.strip()
    lower = text.lower()

    if lower in ("/start", "/start@bakkalbot"):
        send(token, chat_id,
             "👋 <b>Merhaba! Bakkal Fiyat Botuna hoş geldiniz!</b> 🛒\n\n"
             "Ben size en güncel market fiyatlarını karşılaştırmalı olarak getiriyorum. "
             "Türkçe veya İngilizce yazabilirsiniz!\n\n"
             "✏️ <b>Nasıl kullanılır?</b>\n"
             "Sadece ürün adını yazın, gerisini ben hallederim:\n\n"
             "<code>süt</code>  <code>ekmek</code>  <code>yağ</code>  <code>şeker</code>  <code>çay</code>\n"
             "<code>milk</code>  <code>bread</code>  <code>oil</code>  <code>cheese</code>  <code>eggs</code>\n\n"
             "📋 <b>Komutlar:</b>\n"
             "/sor &lt;soru&gt; — AI asistana herhangi bir soru sor 🤖\n"
             "/firsat — Bugünün en iyi fırsatları 🔥\n"
             "/markets — Takip ettiğim marketler\n"
             "/son — Son güncellenen ürünler\n"
             "/help — Yardım\n\n"
             "<i>Fiyatlar her sabah 07:00'de güncellenir</i> ☀️")

    elif lower in ("/help", "/help@bakkalbot"):
        send(token, chat_id,
             "🤝 <b>Size nasıl yardımcı olabilirim?</b>\n\n"
             "Aklınızdaki ürünü yazmanız yeterli — Türkçe ya da İngilizce:\n\n"
             "<code>süt</code> veya <code>milk</code> → tüm marketlerde süt fiyatları\n"
             "<code>yağ</code> veya <code>oil</code> → ayçiçek, zeytinyağı ve daha fazlası\n"
             "<code>200ml süt</code> → daha spesifik arama\n\n"
             "📋 <b>Tüm komutlar:</b>\n"
             "/sor &lt;soru&gt; — AI asistana herhangi bir soru sor 🤖\n"
             "/fiyat &lt;ürün&gt; — Fiyat sorgula\n"
             "/firsat — Bugünün en iyi fırsatları\n"
             "/markets — Takip edilen marketler\n"
             "/son — Son güncellenen 10 ürün\n"
             "/help — Bu yardım mesajı\n\n"
             "💬 <i>Herhangi bir sorunuz olursa yazmaktan çekinmeyin!</i>")

    elif lower in ("/markets", "/markets@bakkalbot"):
        markets = get_all_markets(supabase)
        send(token, chat_id, build_markets_reply(markets))

    elif lower in ("/son", "/son@bakkalbot"):
        rows = get_recent_products(supabase)
        send(token, chat_id, build_recent_reply(rows))

    elif lower in ("/firsat", "/firsat@bakkalbot", "/fırsat", "/fırsat@bakkalbot"):
        rows = get_best_deals(supabase)
        send(token, chat_id, build_deals_reply(rows))

    elif lower.startswith("/fiyat "):
        query = text[7:].strip()
        if not query:
            send(token, chat_id,
                 "🤔 Hangi ürünü aramak istersiniz?\n\n"
                 "Kullanım: <code>/fiyat süt</code>\n\n"
                 "Örnekler: <code>süt</code>, <code>ekmek</code>, <code>yağ</code>, <code>şeker</code>")
            return
        rows, matched = search_prices(supabase, query)
        history = get_price_history(supabase, rows[0]["product_url"]) if rows else []
        send(token, chat_id, build_price_reply(query, rows, matched, history))

    elif lower in ("merhaba", "selam", "hi", "hello", "hey", "sa", "slm"):
        send(token, chat_id,
             "👋 <b>Merhaba!</b> Nasılsınız?\n\n"
             "Bugün hangi ürünün fiyatına bakmak istersiniz? "
             "Türkçe veya İngilizce yazabilirsiniz 😊\n\n"
             "Örnek: <code>süt</code>, <code>ekmek</code>, <code>milk</code>, <code>bread</code>")

    elif lower in ("teşekkür", "teşekkürler", "sağol", "sağolun",
                   "thanks", "thank you", "thx", "ty"):
        send(token, chat_id,
             "😊 Rica ederim! Başka bir ürün sormak ister misiniz?\n\n"
             "<code>süt</code>  <code>ekmek</code>  <code>yağ</code>  <code>şeker</code>  <code>çay</code>")

    elif lower in ("iyi günler", "güle güle", "bye", "görüşürüz"):
        send(token, chat_id,
             "👋 İyi günler! Fiyat karşılaştırması için tekrar bekleriz 🛒")

    elif lower.startswith("/sor ") or lower.startswith("/sor@"):
        # /sor <natural language question> — AI-powered chat
        question = text[4:].strip() if lower.startswith("/sor ") else text.split(" ", 1)[1].strip() if " " in text else ""
        if not question:
            send(token, chat_id,
                 "🤖 <b>AI Asistan</b>\n\n"
                 "Bana herhangi bir soru sorabilirsiniz:\n\n"
                 "<code>/sor en ucuz süt hangi markette?</code>\n"
                 "<code>/sor bugün hangi ürünlerde indirim var?</code>\n"
                 "<code>/sor BİM'de ekmek kaç lira?</code>")
            return
        send(token, chat_id, "🤖 Düşünüyorum...")
        reply = chat_with_data(supabase, openai_client, question, chat_id)
        send(token, chat_id, f"🤖 <b>AI Asistan:</b>\n\n{_esc(reply)}")

    elif lower in ("/sor", "/sor@bakkalbot"):
        send(token, chat_id,
             "🤖 <b>AI Asistan</b>\n\n"
             "Bana herhangi bir soru sorabilirsiniz:\n\n"
             "<code>/sor en ucuz süt hangi markette?</code>\n"
             "<code>/sor bugün hangi ürünlerde indirim var?</code>\n"
             "<code>/sor BİM'de ekmek kaç lira?</code>")

    elif lower.startswith("/"):
        send(token, chat_id,
             "🤔 Bu komutu tanımıyorum.\n\n"
             "Yardım için /help yazabilirsiniz.")

    else:
        # Treat any plain text as a product query
        rows, matched = search_prices(supabase, text)
        display_query = _clean_query(text)
        history = get_price_history(supabase, rows[0]["product_url"]) if rows else []
        send(token, chat_id, build_price_reply(display_query, rows, matched, history))


# ─────────────────────────────────────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────────────────────────────────────

def run_bot() -> None:
    config = load_config()
    token = config["TELEGRAM_BOT_TOKEN"]
    supabase = create_client(config["SUPABASE_URL"], config["SUPABASE_KEY"])
    openai_client = OpenAI(api_key=config["OPENAI_API_KEY"])

    logger.info("=== Bakkal Price Bot starting (long-poll mode) ===")

    # Get the bot's own username for logging
    me = tg(token, "getMe")
    bot_name = me.get("result", {}).get("username", "unknown")
    logger.info(f"Bot running as @{bot_name}")

    # Skip any updates that arrived while the bot was offline (stale messages).
    # A single getUpdates with timeout=0 and offset=-1 returns at most the last
    # update; advancing past it prevents replying to old messages on restart.
    try:
        stale = tg(token, "getUpdates", offset=-1, timeout=0)
        stale_results = stale.get("result", [])
        if stale_results:
            offset = stale_results[-1]["update_id"] + 1
            logger.info(f"Skipped {len(stale_results)} stale update(s), starting at offset {offset}")
        else:
            offset = 0
    except Exception:
        offset = 0

    global _bot_alive
    _bot_alive = True

    while True:
        try:
            updates = get_updates(token, offset)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            _bot_alive = False
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
                handle_message(token, supabase, openai_client, chat_id, text)
            except Exception as exc:
                logger.error(f"handle_message error: {exc}")
                try:
                    send(token, chat_id, "❌ Bir hata oluştu, lütfen tekrar deneyin.")
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Minimal HTTP server — required by Render Web Service to bind a port
# ─────────────────────────────────────────────────────────────────────────────

_bot_alive = False  # Set True once polling loop is running


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        status = b"OK - bot alive" if _bot_alive else b"STARTING"
        self.wfile.write(status)

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
    url = f"http://127.0.0.1:{port}/"
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
    # Run Telegram bot on main thread — restart automatically on any crash
    while True:
        try:
            run_bot()
        except KeyboardInterrupt:
            logger.info("Bot shut down by user.")
            break
        except Exception as exc:
            logger.error(f"run_bot() crashed: {exc} — restarting in 10 s")
            _bot_alive = False
            time.sleep(10)
