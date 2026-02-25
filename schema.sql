-- ============================================================
-- Bakkal Price Monitoring — Supabase Schema
-- Run this in the Supabase SQL Editor before first use.
-- ============================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Price history table
CREATE TABLE IF NOT EXISTS price_history (
    id              UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
    product_url     TEXT         NOT NULL,
    product_name    TEXT         NOT NULL,
    market_name     TEXT         NOT NULL,
    current_price   NUMERIC(10, 2) NOT NULL,
    previous_price  NUMERIC(10, 2),
    price_drop_pct  NUMERIC(5, 2),
    -- scraped_date is stored as a real DATE column (not computed) so that
    -- Supabase upsert on_conflict="product_url,scraped_date" works correctly.
    scraped_date    DATE         NOT NULL DEFAULT CURRENT_DATE,
    scraped_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- One record per product per day; upsert updates it if re-run same day
    CONSTRAINT uq_product_url_date UNIQUE (product_url, scraped_date)
);

-- Fast lookup by product URL (most frequent query)
CREATE INDEX IF NOT EXISTS idx_price_history_product_url
    ON price_history (product_url);

-- Time-series / reporting queries
CREATE INDEX IF NOT EXISTS idx_price_history_scraped_at
    ON price_history (scraped_at DESC);

-- Per-market filtering
CREATE INDEX IF NOT EXISTS idx_price_history_market_name
    ON price_history (market_name);

-- ── Row Level Security ────────────────────────────────────────
-- Enable RLS so anonymous keys can't access the table directly.
-- The service_role key bypasses RLS automatically, so our script
-- (which uses SUPABASE_KEY = service_role) has full access.
ALTER TABLE price_history ENABLE ROW LEVEL SECURITY;

-- Allow service role (server-side scripts) unrestricted access
CREATE POLICY "service_role_full_access"
    ON price_history
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);
