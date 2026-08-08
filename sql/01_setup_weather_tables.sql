-- Homework 2: Environment Agency warning documents + pgvector embeddings
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_documents (
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    county TEXT,
    query_latitude DOUBLE PRECISION,
    query_longitude DOUBLE PRECISION,
    source_type TEXT NOT NULL CHECK (source_type = 'alert'),
    headline TEXT NOT NULL,
    narrative_text TEXT NOT NULL,
    severity TEXT,
    severity_level SMALLINT CHECK (
        severity_level IS NULL OR severity_level BETWEEN 1 AND 4
    ),
    flood_area_id TEXT,
    river_or_sea TEXT,
    ea_area_name TEXT,
    ea_region_name TEXT,
    source_url TEXT,
    issued_at TIMESTAMPTZ,
    effective_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_weather_documents_county
    ON weather_documents (county);
CREATE INDEX IF NOT EXISTS idx_weather_documents_severity
    ON weather_documents (severity_level);
CREATE INDEX IF NOT EXISTS idx_weather_documents_effective_at
    ON weather_documents (effective_at DESC);

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES weather_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    chunk_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding VECTOR(384) NOT NULL,
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index, model_name)
);

CREATE INDEX IF NOT EXISTS idx_weather_embeddings_document_id
    ON weather_embeddings (document_id);
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_embedding
    ON weather_embeddings USING hnsw (embedding vector_cosine_ops);
