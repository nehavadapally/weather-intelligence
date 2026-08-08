-- Homework 2, Step 1: create the raw NWS weather document table.
-- Run this file manually in the Lakebase Postgres SQL editor.
--
-- Drop existing table if you need to recreate:
-- DROP TABLE IF EXISTS weather_documents CASCADE;

CREATE TABLE IF NOT EXISTS weather_documents (
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    source_type TEXT NOT NULL CHECK (source_type IN ('alert', 'forecast')),
    headline TEXT NOT NULL,
    narrative_text TEXT NOT NULL,
    issued_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_weather_documents_location
    ON weather_documents (location);

CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type
    ON weather_documents (source_type);

CREATE INDEX IF NOT EXISTS idx_weather_documents_effective_at
    ON weather_documents (effective_at DESC);

CREATE INDEX IF NOT EXISTS idx_weather_documents_synced_at
    ON weather_documents (synced_at DESC);
