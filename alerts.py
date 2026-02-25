"""
alerts.py — Telegram Bot notifications.

send_price_drop_alert()  — BUY alert when price drops below threshold
send_daily_summary()     — end-of-run summary (always sent)

Uses raw HTTP POST to the Telegram Bot API via requests.
No heavy bot framework — keeps dependencies minimal.
"""

import logging
from datetime import datetime, timezone

import requests

from parser import ProductData

logger = logging.getLogger("bakkal_monitor.alerts")

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _fmt_price(price: float) -> str:
    """Format a float as Turkish lira string, e.g. 1249.99 → '1.249,99 TL'."""
    # Python formats with comma as thousands sep and period as decimal
    international = f"{price:,.2f}"   # e.g. "1,249.99"
    # Swap separators to Turkish style
    turkish = (
        international
        .replace(",", "X")   # temporary placeholder
        .replace(".", ",")   # period → comma (decimal)
        .replace("X", ".")   # placeholder → period (thousands)
    )
    return f"{turkish} TL"


def send_price_drop_alert(
    bot_token: str,
    chat_id: str,
    product: ProductData,
    previous_price: float,
    drop_pct: float,
) -> bool:
    """
    Send a Telegram BUY alert for a detected price drop.
    Message is formatted in Turkish using Markdown.
    Returns True if delivered successfully, False otherwise.
    """
    prev_fmt = _fmt_price(previous_price)
    curr_fmt = _fmt_price(product.current_price)

    message = (
        f"*Fiyat Düşüşü Alarmı!*\n\n"
        f"*{product.product_name}*\n"
        f"Market: {product.market_name}\n"
        f"Önceki Fiyat: {prev_fmt}\n"
        f"Yeni Fiyat: *{curr_fmt}*\n"
        f"Düşüş: *%{drop_pct:.1f}*\n\n"
        f"[Ürüne Git]({product.product_url})"
    )

    url = _TELEGRAM_API.format(token=bot_token)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        logger.info(
            f"Telegram alert sent: {product.product_name!r} "
            f"({drop_pct:.1f}% drop)"
        )
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
    Send an end-of-run summary message so you can confirm the cron ran.
    Never raises — silently logs on failure.
    """
    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    message = (
        f"*Bakkal Monitor — Günlük Rapor*\n"
        f"Tarih: {now_str}\n\n"
        f"Taranan ürün: {total_scraped}\n"
        f"Fiyat düşüşü alarmı: {total_alerts}\n"
        f"Hata: {total_errors}"
    )

    url = _TELEGRAM_API.format(token=bot_token)
    try:
        requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=15,
        )
        logger.info("Daily summary sent to Telegram.")
    except requests.RequestException as exc:
        logger.error(f"Telegram send_daily_summary error: {exc}")
