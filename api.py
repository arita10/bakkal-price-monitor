"""
api.py — FastAPI REST interface for the Bakkal Price Monitor.

Exposes the same core logic (scraper, parser, storage) as HTTP endpoints
so your own application can integrate with the system.

Run locally:
    uvicorn api:app --reload --port 8000

Endpoints:
    GET  /                          Health check
    GET  /prices                    Latest price for every tracked product
    GET  /prices/{market}           Latest prices filtered by market name
    GET  /prices/product?url=...    Full price history for one product URL
    GET  /alerts                    All recorded price drops (drop_pct < 0)
    POST /run                       Trigger a full scrape + parse + store cycle
    GET  /markets                   List of distinct market names in DB
"""

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client

from config import load_config
from main import run as run_monitor

logger = logging.getLogger("bakkal_monitor.api")

# ─────────────────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Bakkal Price Monitor API",
    description=(
        "Track daily grocery prices from Turkish discount markets "
        "(BİM, A101, Şok, Migros, CarrefourSA) and receive BUY alerts."
    ),
    version="1.0.0",
)

# Allow all origins so any frontend / mobile app can connect.
# Restrict origins in production by replacing ["*"] with your domain list.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────────────────────────────────────

_config: dict = {}
_supabase: Optional[Client] = None


@app.on_event("startup")
def startup():
    global _config, _supabase
    _config = load_config()
    _supabase = create_client(_config["SUPABASE_URL"], _config["SUPABASE_KEY"])
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger.info("Bakkal API started.")


# ─────────────────────────────────────────────────────────────────────────────
# Response schemas
# ─────────────────────────────────────────────────────────────────────────────

class PriceRecord(BaseModel):
    id: str
    product_url: str
    product_name: str
    market_name: str
    current_price: float
    previous_price: Optional[float]
    price_drop_pct: Optional[float]
    scraped_date: str
    scraped_at: str


class RunResponse(BaseModel):
    status: str
    message: str


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def health():
    """Health check — confirms the API is running."""
    return {"status": "ok", "service": "Bakkal Price Monitor"}


@app.get("/markets", tags=["Prices"], response_model=list[str])
def get_markets():
    """Return all distinct market names currently in the database."""
    try:
        resp = (
            _supabase.table("price_history")
            .select("market_name")
            .execute()
        )
        names = sorted({row["market_name"] for row in resp.data})
        return names
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/prices", tags=["Prices"], response_model=list[PriceRecord])
def get_latest_prices(
    limit: int = Query(100, ge=1, le=500, description="Max records to return"),
):
    """
    Return the most recent price record for every tracked product,
    ordered by scraped_at descending.
    """
    try:
        resp = (
            _supabase.table("price_history")
            .select("*")
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/prices/{market}", tags=["Prices"], response_model=list[PriceRecord])
def get_prices_by_market(
    market: str,
    limit: int = Query(100, ge=1, le=500),
):
    """
    Return latest prices filtered by market name (case-insensitive).
    Example: /prices/Migros  or  /prices/BIM
    """
    try:
        resp = (
            _supabase.table("price_history")
            .select("*")
            .ilike("market_name", market)
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        if not resp.data:
            raise HTTPException(
                status_code=404,
                detail=f"No prices found for market '{market}'.",
            )
        return resp.data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/prices/product/history", tags=["Prices"], response_model=list[PriceRecord])
def get_product_history(
    url: str = Query(..., description="Full product URL to look up"),
    limit: int = Query(30, ge=1, le=365),
):
    """
    Return full price history for a single product URL,
    oldest-to-newest (useful for charting price trends).
    """
    try:
        resp = (
            _supabase.table("price_history")
            .select("*")
            .eq("product_url", url)
            .order("scraped_at", desc=False)
            .limit(limit)
            .execute()
        )
        if not resp.data:
            raise HTTPException(
                status_code=404,
                detail="No history found for this product URL.",
            )
        return resp.data
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/alerts", tags=["Alerts"], response_model=list[PriceRecord])
def get_price_drops(
    min_drop_pct: float = Query(
        5.0, ge=0.1, description="Minimum drop % to include"
    ),
    limit: int = Query(50, ge=1, le=200),
):
    """
    Return all recorded price drops where price_drop_pct >= min_drop_pct,
    newest first. Use this to review historical BUY alerts.
    """
    try:
        resp = (
            _supabase.table("price_history")
            .select("*")
            .gte("price_drop_pct", min_drop_pct)
            .order("scraped_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/run", tags=["Control"], response_model=RunResponse)
async def trigger_run(background_tasks: BackgroundTasks):
    """
    Manually trigger a full scrape → parse → store → alert cycle.
    Runs in the background so the HTTP response returns immediately.
    Check your Telegram for the daily summary when it finishes.
    """
    background_tasks.add_task(_run_in_background)
    return RunResponse(
        status="accepted",
        message=(
            "Price monitoring run started in the background. "
            "You will receive a Telegram summary when it completes."
        ),
    )


async def _run_in_background():
    try:
        await run_monitor()
    except Exception as exc:
        logger.error(f"Background run failed: {exc}")
