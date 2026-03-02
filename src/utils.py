"""
src/utils.py — Shared helper functions.

  parse_tr_price()  — Convert Turkish price string to float
  chunk_text()      — Split long text into fixed-size chunks
"""

import re


def parse_tr_price(raw: str) -> float:
    """
    Convert Turkish price string to float.
    '84,90 ₺'      -> 84.90
    '1.249,90 ₺'   -> 1249.90
    '17,90₺'       -> 17.90
    'İyi Fiyat\n11,95 TL' -> 11.95

    Strategy: find the last number that looks like X,XX (Turkish decimal).
    Falls back to stripping all non-numeric chars.
    """
    # Primary: find last Turkish-format number (optional thousands dot + comma decimal)
    matches = re.findall(r"[\d][.\d]*,\d+", raw)
    if matches:
        cleaned = matches[-1].replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            pass

    # Fallback: strip currency symbols / labels and parse plain number
    cleaned = raw.replace("₺", "").replace("TL", "").replace("\xa0", "").strip()
    cleaned = cleaned.split("\n")[-1].strip()
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


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
