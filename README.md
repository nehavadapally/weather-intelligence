# Homework 2: Weather Intelligence

This project adapts the Databricks Day 2 Lakebase vector-search pattern from
financial news to unstructured weather text.

The application:

1. Harvests active alerts and narrative forecasts from the US National Weather
   Service API.
2. Normalises the responses into a `weather_documents` table in Lakebase.
3. Chunks and embeds the narrative text with
   `sentence-transformers/all-MiniLM-L6-v2`.
4. Stores 384-dimensional vectors in the `weather_embeddings` pgvector table.
5. Retrieves the most relevant chunks through `POST /weather/search`.

## Data source

The project uses the National Weather Service API because it:

- is public and does not require an API key;
- provides rich free-text alert descriptions, instructions and forecasts;
- supports active alerts and multi-period forecasts from the same source;
- provides raw JSON that can be retained for provenance.

City and state names are resolved to coordinates with OpenStreetMap Nominatim.
Latitude and longitude strings can also be supplied directly.

The main limitation is that NWS covers the United States and its territories.

## Repository structure

```text
weather-intelligence/
├── .env.example
├── .gitignore
├── HOMEWORK_STEPS.md
├── README.md
├── app.py
├── app.yaml
├── lakebase.py
├── llm_summary.py
├── requirements-dev.txt
├── requirements.txt
├── setup_secrets.py
├── text_utils.py
├── weather_client.py
├── weather_sync.py
├── notebooks/
│   └── ingest_weather_embeddings.ipynb
├── sql/
│   ├── 01_setup_weather_documents_tables.sql
│   └── 02_setup_weather_embeddings_table.sql
├── templates/
│   └── index.html
└── tests/
    ├── test_chunking.py
    ├── test_llm_summary.py
    ├── test_weather_client.py
    └── test_weather_sync.py
```

## Schema

### `weather_documents`

| Column | Purpose |
|---|---|
| `id` | Stable NWS alert ID or deterministic forecast hash |
| `location` | Resolved location label |
| `latitude`, `longitude` | Coordinates used for NWS requests |
| `source_type` | `alert` or `forecast` |
| `headline` | Alert headline or forecast period name |
| `narrative_text` | Free text used for embedding |
| `issued_at`, `effective_at` | Source timestamps |
| `payload` | Original JSON response |
| `synced_at` | Last meaningful insert or update time |

### `weather_embeddings`

| Column | Purpose |
|---|---|
| `id` | Stable embedding-row ID |
| `document_id` | Foreign key to `weather_documents.id` |
| `chunk_index` | Position of the chunk |
| `chunk_text` | Text represented by the vector |
| `content_hash` | Detects changed source text |
| `embedding` | `VECTOR(384)` |
| `model_name` | Embedding model |
| `created_at` | Vector write timestamp |

The embedding table has an HNSW index using `vector_cosine_ops`.

## Chunking and embedding

The notebook uses:

- model: `sentence-transformers/all-MiniLM-L6-v2`;
- vector size: 384;
- chunk size: 800 characters;
- overlap: 100 characters;
- direct psycopg2 writes with `%s::vector`.

The Databricks runtime-provided `psycopg2` package is used. The notebook does
not install or uninstall it.

## Run the pipeline

### 1. Create the Lakebase tables

Run these SQL files in order:

```text
sql/01_setup_weather_documents_tables.sql
sql/02_setup_weather_embeddings_table.sql
```

### 2. Configure the Lakebase secret

Store the PostgreSQL URL under:

```text
scope: database
key: lakebase-url
```

The URL should follow this format:

```text
postgresql://<role>:<password>@<host>:5432/<database>?sslmode=require
```

### 3. Start or deploy the Flask application

```bash
python app.py
```

Health check:

```bash
curl http://localhost:8000/healthz
```

### 4. Harvest weather documents

Using city and state names:

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations":["Chicago, IL","Austin, TX"],"limit":50}'
```

Using coordinates avoids the separate geocoding request:

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations":["41.8781,-87.6298"],"limit":50}'
```

### 5. Create embeddings

Run:

```text
notebooks/ingest_weather_embeddings.ipynb
```

The notebook reads raw documents, skips unchanged text, creates chunks,
generates embeddings and writes vectors to `weather_embeddings`.

### 6. Search the embeddings

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query":"flash flood risk near rivers","top_k":5}'
```

Optional source filter:

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query":"immediate action required","top_k":5,"source_type":"alert"}'
```

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/healthz` | Application health |
| `POST` | `/weather/sync` | Harvest and upsert weather documents |
| `GET` | `/weather/documents` | Inspect stored raw documents |
| `POST` | `/weather/search` | Semantic search over weather chunks |
| `GET` | `/weather/search` | Browser-friendly search with optional summary |

The LLM summary is optional and fails softly. Vector-search results are still
returned when no serving endpoint is configured.

## Implemented safeguards

- Stable document IDs and `ON CONFLICT` upserts
- Batched writes with `execute_values`
- Per-location error collection
- Query validation
- `top_k` constrained to 1 through 20
- Shared embedding model between ingestion and search
- Content-hash detection for changed documents
- Stale chunk deletion before re-embedding
- HNSW cosine-similarity index

## Known limitations

- NWS coverage is limited to the United States and its territories.
- City-name ingestion depends on Nominatim availability.
- Forecast documents change frequently and should be refreshed before a demo.
- The optional LLM summary requires a permitted Databricks model-serving
  endpoint.
- The embedding process is run separately from `/weather/sync`.
