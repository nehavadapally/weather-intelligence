-- Homework 2, Step 2: create the pgvector weather embedding table.
-- Run this file manually after 01_setup_weather_documents_table.sql.
-- The 384 dimensions match sentence-transformers/all-MiniLM-L6-v2.
--
-- Drop existing table if you need to recreate:
-- DROP TABLE IF EXISTS weather_embeddings CASCADE;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS weather_embeddings (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL
        REFERENCES weather_documents(id)
        ON DELETE CASCADE,
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

-- Index on model_name for filtering
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_model_name
    ON weather_embeddings (model_name);

-- HNSW index for fast vector similarity search (cosine distance)
CREATE INDEX IF NOT EXISTS idx_weather_embeddings_hnsw
    ON weather_embeddings
    USING hnsw (embedding vector_cosine_ops);
