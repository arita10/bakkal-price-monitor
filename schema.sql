-- ============================================================
-- Bakkal Price Monitoring — Supabase Schema v2
-- Run this in the Supabase SQL Editor.
--
-- Design:
--   products        — one row per unique product URL (master registry)
--   price_history   — one row per product per day (time-series log)
--   v_latest_prices — view: most recent price per product (for bot queries)
--   v_price_trend   — view: last 30 days price history per product
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─────────────────────────────────────────────────────────────
-- TABLE: products
-- Master registry — one row per unique product URL.
-- Updated in-place when product name or market changes.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id              UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
    product_url     TEXT         NOT NULL UNIQUE,
    product_name    TEXT         NOT NULL,
    market_name     TEXT         NOT NULL,
    -- Denormalised latest price for fast single-row lookups
    latest_price    NUMERIC(10, 2),
    first_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_products_market
    ON products (market_name);

CREATE INDEX IF NOT EXISTS idx_products_name
    ON products USING gin (to_tsvector('simple', product_name));


-- ─────────────────────────────────────────────────────────────
-- TABLE: price_history
-- Pure time-series — one row per product per day.
-- Never updated; new rows are inserted daily.
-- previous_price and price_drop_pct are computed at insert time.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS price_history (
    id              UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
    product_url     TEXT         NOT NULL,
    product_name    TEXT         NOT NULL,
    market_name     TEXT         NOT NULL,
    current_price   NUMERIC(10, 2) NOT NULL,
    previous_price  NUMERIC(10, 2),           -- price from the previous day's row
    price_drop_pct  NUMERIC(6, 2),            -- positive = cheaper, negative = more expensive
    scraped_date    DATE         NOT NULL DEFAULT CURRENT_DATE,
    scraped_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- One record per product per day; re-runs on same day update price
    CONSTRAINT uq_product_url_date UNIQUE (product_url, scraped_date)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_ph_product_url
    ON price_history (product_url);

CREATE INDEX IF NOT EXISTS idx_ph_scraped_at
    ON price_history (scraped_at DESC);

CREATE INDEX IF NOT EXISTS idx_ph_market_name
    ON price_history (market_name);

CREATE INDEX IF NOT EXISTS idx_ph_scraped_date
    ON price_history (scraped_date DESC);

-- Partial index: quickly find price drops >= 5%
CREATE INDEX IF NOT EXISTS idx_ph_price_drops
    ON price_history (price_drop_pct DESC)
    WHERE price_drop_pct >= 5;


-- ─────────────────────────────────────────────────────────────
-- VIEW: v_latest_prices
-- Most recent scraped price per product.
-- Used by the Telegram bot for price queries.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_latest_prices AS
SELECT DISTINCT ON (product_url)
    product_url,
    product_name,
    market_name,
    current_price,
    previous_price,
    price_drop_pct,
    scraped_date,
    scraped_at
FROM price_history
ORDER BY product_url, scraped_at DESC;


-- ─────────────────────────────────────────────────────────────
-- VIEW: v_price_trend
-- Last 30 days of price history per product, ordered newest first.
-- Used by the bot's /trend command and history display.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_price_trend AS
SELECT
    product_url,
    product_name,
    market_name,
    current_price,
    previous_price,
    price_drop_pct,
    scraped_date,
    scraped_at
FROM price_history
WHERE scraped_date >= CURRENT_DATE - INTERVAL '30 days'
ORDER BY product_url, scraped_date DESC;


-- ─────────────────────────────────────────────────────────────
-- VIEW: v_best_deals
-- Today's biggest price drops across all markets.
-- Used by the bot's /firsat command.
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_best_deals AS
SELECT
    product_url,
    product_name,
    market_name,
    current_price,
    previous_price,
    price_drop_pct,
    scraped_date
FROM price_history
WHERE scraped_date = CURRENT_DATE
  AND price_drop_pct >= 5
ORDER BY price_drop_pct DESC
LIMIT 20;


-- ─────────────────────────────────────────────────────────────
-- Row Level Security
-- ─────────────────────────────────────────────────────────────
ALTER TABLE products      ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;

CREATE POLICY "service_role_full_access" ON products
    FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY "service_role_full_access" ON price_history
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ─────────────────────────────────────────────────────────────
-- Migration note:
-- If upgrading from v1 (price_history only), run this to
-- backfill the products table from existing price_history rows:
--
-- INSERT INTO products (product_url, product_name, market_name,
--                       latest_price, first_seen_at, last_seen_at)
-- SELECT DISTINCT ON (product_url)
--     product_url, product_name, market_name,
--     current_price, MIN(scraped_at) OVER (PARTITION BY product_url),
--     MAX(scraped_at) OVER (PARTITION BY product_url)
-- FROM price_history
-- ORDER BY product_url, scraped_at DESC
-- ON CONFLICT (product_url) DO NOTHING;
-- ─────────────────────────────────────────────────────────────
