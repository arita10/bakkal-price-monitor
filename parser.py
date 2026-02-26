"""
parser.py — OpenAI GPT-4o Mini price extraction.

Uses the OpenAI SDK with structured JSON output (response_format).
Sends Markdown/JSON content chunks and returns ProductData objects.
"""

import json
import logging

from openai import OpenAI
from pydantic import BaseModel, Field

from scraper import ProductRaw

logger = logging.getLogger("bakkal_monitor.parser")

# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────

class ProductData(BaseModel):
    """Structured product data extracted by AI from raw content."""
    product_name: str = Field(description="Full product name in Turkish")
    current_price: float = Field(
        description=(
            "Numeric price in Turkish Lira. "
            "Convert Turkish format: '12,99' -> 12.99  |  '1.249,99' -> 1249.99"
        )
    )
    market_name: str = Field(
        description=(
            "Retailer name. Recognise: BIM, A101, SOK, "
            "Migros, CarrefourSA, Hakmar, Tarim Kredi"
        )
    )
    product_url: str = Field(
        description="Full product URL. Use source URL if no individual URL available."
    )


# ─────────────────────────────────────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are a Turkish grocery price extraction assistant.
Extract ALL product names, prices, market names, and URLs from the provided content.

CRITICAL price parsing rules:
- Turkish decimal separator is COMMA:       "12,99 TL"    -> 12.99
- Turkish thousands separator is PERIOD:    "1.249,99 TL" -> 1249.99
- Currency markers: "TL", or absent (assume TL).
- Skip any product where you cannot confidently extract BOTH name AND price.

Market names to recognise: BIM, A101, SOK, Migros, CarrefourSA, Hakmar, Tarim Kredi.

Return ONLY a valid JSON object with this exact structure:
{"products": [{"product_name": "...", "current_price": 0.0, "market_name": "...", "product_url": "..."}]}
If nothing is found, return {"products": []}.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Client initialisation
# ─────────────────────────────────────────────────────────────────────────────

def build_gemini_client(api_key: str) -> OpenAI:
    """Return a configured OpenAI client (function name kept for compatibility)."""
    return OpenAI(api_key=api_key)


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_chunk(
    raw: ProductRaw,
    client: OpenAI,
) -> list[ProductData]:
    """
    Send one ProductRaw chunk to GPT-4o Mini and return extracted products.
    Returns [] on any failure (fail-safe).
    """
    if not raw.content.strip():
        return []

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": raw.content},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=4096,
        )

        raw_json = response.choices[0].message.content
        if not raw_json:
            logger.warning(f"Empty response for chunk from {raw.source_url}")
            return []

        data = json.loads(raw_json)
        products = []
        for item in data.get("products", []):
            try:
                p = ProductData(**item)
                if not p.product_url or p.product_url.upper() in ("N/A", "NONE", "NULL", ""):
                    p.product_url = raw.source_url
                products.append(p)
            except Exception:
                continue

        logger.info(
            f"OpenAI: {len(products)} product(s) from "
            f"{raw.source} ({raw.source_url[:60]})"
        )
        return products

    except Exception as exc:
        logger.error(f"OpenAI parse error for chunk from {raw.source_url}: {exc}")
        return []
