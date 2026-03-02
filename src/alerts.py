"""
src/alerts.py — Telegram Bot notifications.

send_price_drop_alert()  — BUY alert when price drops below threshold
send_daily_summary()     — end-of-run summary (always sent)
"""

import logging
from datetime import datetime, timezone

import requests

from src.agents.parser import ProductData

logger = logging.getLogger("bakkal_monitor.alerts")

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_price(price: float) -> str:
    """Format a float as Turkish lira string, e.g. 1249.99 -> '1.249,99 TL'."""
    international = f"{price:,.2f}"
    turkish = (
        international
        .replace(",", "X")
        .replace(".", ",")
        .replace("X", ".")
    )
    return f"{turkish} TL"


def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_price_drop_alert(
    bot_token: str,
    chat_id: str,
    product: ProductData,
    previous_price: float,
    drop_pct: float,
) -> bool:
    """
    Send a Telegram BUY alert for a detected price drop.
    Returns True if delivered successfully, False otherwise.
    """
    prev_fmt = _fmt_price(previous_price)
    curr_fmt = _fmt_price(product.current_price)

    message = (
        f"📉 <b>Fiyat Düşüşü Alarmı!</b>\n\n"
        f"<b>{_esc(product.product_name)}</b>\n"
        f"Market: {_esc(product.market_name)}\n"
        f"Önceki Fiyat: {_esc(prev_fmt)}\n"
        f"Yeni Fiyat: <b>{_esc(curr_fmt)}</b>\n"
        f"Düşüş: <b>%{drop_pct:.1f}</b>\n\n"
        f'<a href="{_esc(product.product_url)}">Ürüne Git</a>'
    )

    url = _TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info(f"Telegram alert sent: {product.product_name!r} ({drop_pct:.1f}% drop)")
        return True
    except requests.RequestException as exc:
        logger.error(f"Telegram send_price_drop_alert error: {exc}")
        return False


def send_daily_summary(
    bot_token: str,
    chat_id: str,
    total_scraped: int,
    total_alerts: int,
    total_errors: int,
) -> None:
    """
    Send an end-of-run summary message.
    Never raises — silently logs on failure.
    """
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    message = (
        f"<b>Bakkal Monitor — Günlük Rapor</b>\n"
        f"Tarih: {now_str}\n\n"
        f"Taranan ürün: {total_scraped}\n"
        f"Fiyat düşüşü alarmı: {total_alerts}\n"
        f"Hata: {total_errors}"
    )

    url = _TELEGRAM_API.format(token=bot_token)
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=15,
        )
        logger.info("Daily summary sent to Telegram.")
    except requests.RequestException as exc:
        logger.error(f"Telegram send_daily_summary error: {exc}")
