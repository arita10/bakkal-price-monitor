-- ============================================================
-- Bakkal Price Monitoring — Supabase Schema v2
-- Run this in the Supabase SQL Editor.
-- Safe to re-run: uses IF NOT EXISTS / CREATE OR REPLACE / DROP IF EXISTS.
--
-- Execution order matters:
--   1. Extensions
--   2. Tables (products first, then price_history)
--   3. Indexes
--   4. Views
--   5. RLS (tables must exist before ALTER TABLE / CREATE POLICY)
--   6. Backfill migration
-- ============================================================

-- ── 1. Extensions ────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ── 2. Tables ────────────────────────────────────────────────

-- products: master registry — one row per unique product URL.
-- Updated in-place whenever latest_price or name changes.
CREATE TABLE IF NOT EXISTS products (
    id              UUID           DEFAULT gen_random_uuid() PRIMARY KEY,
    product_url     TEXT           NOT NULL UNIQUE,
    product_name    TEXT           NOT NULL,
    market_name     TEXT           NOT NULL,
    latest_price    NUMERIC(10, 2),                        -- denormalised for fast lookup
    first_seen_at   TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- price_history: pure time-series — one row per product per day.
-- Rows are never deleted; use upsert to update same-day rows.
CREATE TABLE IF NOT EXISTS price_history (
    id              UUID           DEFAULT gen_random_uuid() PRIMARY KEY,
    product_url     TEXT           NOT NULL,
    product_name    TEXT           NOT NULL,
    market_name     TEXT           NOT NULL,
    current_price   NUMERIC(10, 2) NOT NULL,
    previous_price  NUMERIC(10, 2),                        -- last known price before today
    price_drop_pct  NUMERIC(6, 2),                         -- positive = cheaper, negative = pricier
    scraped_date    DATE           NOT NULL DEFAULT CURRENT_DATE,
    scraped_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_product_url_date UNIQUE (product_url, scraped_date)
);


-- ── 3. Indexes ────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_products_market
    ON products (market_name);

CREATE INDEX IF NOT EXISTS idx_products_name
    ON products USING gin (to_tsvector('simple', product_name));

CREATE INDEX IF NOT EXISTS idx_ph_product_url
    ON price_history (product_url);

CREATE INDEX IF NOT EXISTS idx_ph_scraped_at
    ON price_history (scraped_at DESC);

CREATE INDEX IF NOT EXISTS idx_ph_market_name
    ON price_history (market_name);

CREATE INDEX IF NOT EXISTS idx_ph_scraped_date
    ON price_history (scraped_date DESC);

-- Partial index: fast lookup for price-drop alerts (>= 5%)
CREATE INDEX IF NOT EXISTS idx_ph_price_drops
    ON price_history (price_drop_pct DESC)
    WHERE price_drop_pct >= 5;


-- ── 4. Views ─────────────────────────────────────────────────

-- v_latest_prices: most recent price per product (bot search queries)
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


-- v_price_trend: last 30 days per product (history display)
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


-- v_best_deals: today's biggest price drops (/firsat command)
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


-- ── 5. Row Level Security ─────────────────────────────────────
-- Must come AFTER tables are created.

ALTER TABLE products      ENABLE ROW LEVEL SECURITY;
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "service_role_full_access" ON products;
CREATE POLICY "service_role_full_access" ON products
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "service_role_full_access" ON price_history;
CREATE POLICY "service_role_full_access" ON price_history
    FOR ALL TO service_role USING (true) WITH CHECK (true);


-- ── 6. Backfill migration (v1 → v2) ─────────────────────────
-- Populates products table from existing price_history rows.
-- Safe to re-run: ON CONFLICT DO NOTHING skips existing URLs.

INSERT INTO products (
    product_url, product_name, market_name,
    latest_price, first_seen_at, last_seen_at
)
SELECT DISTINCT ON (product_url)
    product_url,
    product_name,
    market_name,
    current_price,
    MIN(scraped_at) OVER (PARTITION BY product_url),
    MAX(scraped_at) OVER (PARTITION BY product_url)
FROM price_history
ORDER BY product_url, scraped_at DESC
ON CONFLICT (product_url) DO NOTHING;
