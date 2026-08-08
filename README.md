# Homework 2: UK Flood Intelligence

This repository repurposes the Databricks Day 2 news-vector-search pattern for a UK public-sector weather use case.

The end-to-end flow is:

1. **Harvest** Environment Agency flood warnings and alerts.
2. **Normalise** each warning into a reusable document schema.
3. **Store** raw documents and original JSON in Lakebase Postgres.
4. **Chunk and embed** warning narratives with `sentence-transformers/all-MiniLM-L6-v2`.
5. **Store** 384-dimensional vectors in Lakebase using pgvector.
6. **Retrieve** the most relevant warning chunks through `POST /weather/search`.

## Why this data source was selected

The project uses the Environment Agency Real-Time Flood Monitoring API:

`https://environment.data.gov.uk/flood-monitoring/id/floods`

It was selected because it:

- is free and does not require an API key or registration;
- returns JSON through a REST-style API;
- includes narrative flood-warning messages suitable for semantic search;
- provides stable flood-area identifiers, severity levels, location descriptions and timestamps;
- is published as open data under the Open Government Licence.

The service covers **England**, rather than the whole UK. This limitation is stated clearly because the repository focuses on Environment Agency flood intelligence, not general UK forecasting.

Required attribution:

> This uses Environment Agency flood and river level data from the real-time data API (Beta).

Official documentation:

`https://environment.data.gov.uk/flood-monitoring/doc/reference`

## Repository structure

```text
.
├── app.py
├── app.yaml
├── lakebase.py
├── weather_client.py
├── text_utils.py
├── setup_secrets.py
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── templates/
│   └── index.html
├── notebooks/
│   └── ingest_weather_embeddings.py
├── sql/
│   └── 01_setup_weather_tables.sql
└── tests/
    ├── test_chunking.py
    └── test_weather_client.py
```

## API input conventions

`POST /weather/sync` accepts a `locations` list. Each entry can be:

- a county or area string, such as `"Somerset"`;
- coordinates, such as `"51.5074,-0.1278"`;
- `"all"`, which requests warnings across England.

Coordinate searches use `radius_km` as the Environment Agency distance filter.

Environment Agency severity levels are:

| Level | Meaning |
|---:|---|
| 1 | Severe Flood Warning: danger to life |
| 2 | Flood Warning: flooding expected, immediate action required |
| 3 | Flood Alert: flooding possible, be prepared |
| 4 | Warning no longer in force |

Use `min_severity: 3` for active levels 1–3. Use `min_severity: 4` when a classroom demonstration needs a wider sample including warnings no longer in force.

## Lakebase schema

### `weather_documents`

| Column | Purpose |
|---|---|
| `id` | Stable deduplication key prefixed with `ea-flood:` |
| `location` | Flood-area description or county |
| `county` | County value returned by the source |
| `query_latitude`, `query_longitude` | Coordinates used when the request was location-radius based |
| `source_type` | `alert` |
| `headline` | Severity plus affected area |
| `narrative_text` | Normalised text used for embedding |
| `severity`, `severity_level` | Environment Agency warning classification |
| `flood_area_id` | Stable source flood-area identifier |
| `river_or_sea` | River or sea name from the nested flood-area object |
| `ea_area_name`, `ea_region_name` | Environment Agency administrative fields |
| `source_url` | Source warning URI |
| `issued_at`, `effective_at` | Warning timestamps |
| `payload` | Complete source JSON for provenance |
| `synced_at` | Last Lakebase upsert time |

### `weather_embeddings`

| Column | Purpose |
|---|---|
| `id` | Stable embedding-row ID |
| `document_id` | Foreign key to `weather_documents.id` |
| `chunk_index` | Position of the chunk in the warning text |
| `chunk_text` | Passage returned by retrieval |
| `content_hash` | Detects changed warning narratives |
| `embedding` | `VECTOR(384)` |
| `model_name` | Sentence-transformer model used |
| `created_at` | Embedding write time |

The ingestion script uses an 800-character chunk size and 100-character overlap. Most warning messages will create one chunk, while longer updates may create several.

# Clear homework steps

## Step 1: Prepare the project

Upload or clone this entire repository into a Databricks Git folder. Keep the folder structure unchanged because the embedding script imports `lakebase.py` from the repository root.

Install the dependencies:

```bash
pip install -r requirements.txt
```

For a Databricks notebook, you can use:

```python
%pip install -r /Workspace/<your-folder>/requirements.txt
```

Restart Python after `%pip` installation if Databricks asks you to do so.

## Step 2: Create Lakebase

1. Open **Lakebase** in the Databricks workspace.
2. Create a database instance.
3. Create a native Postgres role with password authentication.
4. Copy the PostgreSQL connection URL. It should resemble:

```text
postgresql://<role>:<password>@<host>:5432/databricks_postgres?sslmode=require
```

## Step 3: Store the Lakebase URL as a secret

From a Databricks terminal or configured local environment, run:

```bash
python setup_secrets.py
```

The script stores the URL as:

```text
database/lakebase-url
```

The Environment Agency API needs no secret or API key.

## Step 4: Create the database tables

Choose one approach.

### Recommended: run the SQL manually

Open a Postgres SQL editor connected to Lakebase and execute:

```text
sql/01_setup_weather_tables.sql
```

This creates:

- `weather_documents`
- `weather_embeddings`
- the pgvector extension
- supporting indexes
- an HNSW cosine index

### Alternative: allow the Python code to create them

Both `app.py` and the embedding script call `lakebase.ensure_weather_tables()`. The Lakebase role must have permission to create the vector extension and tables.

## Step 5: Run the Flask app

For local testing, copy `.env.example` to `.env` and place the real Lakebase URL in it:

```bash
cp .env.example .env
python app.py
```

The app starts on port `8000` by default.

Health check:

```bash
curl http://localhost:8000/healthz
```

## Step 6: Harvest flood-warning documents

### Fetch active warnings across England

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{
    "locations": ["all"],
    "limit": 100,
    "min_severity": 3
  }'
```

### Fetch by county

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{
    "locations": ["Somerset", "Greater London"],
    "limit": 50,
    "min_severity": 3
  }'
```

### Fetch near coordinates

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{
    "locations": ["51.5074,-0.1278"],
    "radius_km": 50,
    "limit": 50,
    "min_severity": 3
  }'
```

If the source currently has few active warnings, repeat the demo with `"min_severity": 4`. This includes records classified as no longer in force and gives you more text for demonstrating the embedding pipeline.

Check harvested rows:

```bash
curl "http://localhost:8000/weather/documents?limit=20"
```

You can also verify in SQL:

```sql
SELECT id, location, severity, severity_level, synced_at
FROM weather_documents
ORDER BY synced_at DESC;
```

## Step 7: Chunk and create embeddings

Run the required psycopg2-based ingestion script:

```bash
python notebooks/ingest_weather_embeddings.py
```

Optional parameters:

```bash
python notebooks/ingest_weather_embeddings.py \
  --chunk-size 800 \
  --chunk-overlap 100 \
  --encode-batch-size 32 \
  --document-limit 200
```

The script:

1. reads `weather_documents` with psycopg2;
2. detects new or changed narrative text using SHA-256 hashes;
3. chunks the text using an overlapping sliding window;
4. loads `sentence-transformers/all-MiniLM-L6-v2` once;
5. creates 384-dimensional vectors;
6. deletes stale chunks for changed documents;
7. writes vectors with `execute_values` and `%s::vector`;
8. commits the batch to Lakebase.

Verify the embeddings:

```sql
SELECT
    COUNT(*) AS embedding_rows,
    COUNT(DISTINCT document_id) AS embedded_documents
FROM weather_embeddings;
```

## Step 8: Run semantic retrieval

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "flooding may affect roads and nearby properties",
    "top_k": 5
  }'
```

Filter by county:

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "river levels are rising after persistent rainfall",
    "top_k": 5,
    "county": "Somerset"
  }'
```

Filter to more serious warnings, where lower numbers are more severe:

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "immediate action is required",
    "top_k": 5,
    "max_severity_level": 2
  }'
```

The search endpoint embeds the query with the same MiniLM model and ranks chunks using:

```sql
1 - (embedding <=> query_vector)
```

## Step 9: Deploy as a Databricks App

1. Go to **Compute → Apps**.
2. Create a custom app.
3. Select this Git folder as the source.
4. Confirm that `app.yaml` is detected.
5. Grant the app service principal permission to read `database/lakebase-url` if your workspace requires explicit permission.
6. Deploy the app.
7. Open the app URL and use the browser interface to sync and search.

The browser interface labels the embedding stage as step 2 because that stage is run as the separate Python ingestion job.

## Step 10: Demonstrate the homework

A simple demonstration order is:

1. Show an empty or existing `weather_documents` table.
2. Call `POST /weather/sync`.
3. Query `weather_documents` to show raw warning text and JSON payloads.
4. Run `ingest_weather_embeddings.py`.
5. Query `weather_embeddings` to show vector rows.
6. Call `POST /weather/search` with a natural-language risk query.
7. Explain that pgvector returns semantically similar passages rather than exact keyword matches.

## Step 11: Run the checks

```bash
pip install -r requirements-dev.txt
python -m compileall .
pytest -q
```

## Example search response

```json
{
  "query": "flooding may affect roads and nearby properties",
  "top_k": 5,
  "count": 1,
  "filters": {
    "county": null,
    "max_severity_level": null
  },
  "results": [
    {
      "id": "ea-flood:123ABC",
      "location": "River Example at Example Town",
      "county": "Somerset",
      "source_type": "alert",
      "headline": "Flood Alert: River Example at Example Town",
      "severity": "Flood Alert",
      "severity_level": 3,
      "chunk_index": 0,
      "chunk_text": "Severity: Flood Alert...",
      "similarity": 0.71
    }
  ]
}
```

## Known limitations

- The Environment Agency API covers England, not Scotland, Wales or Northern Ireland.
- It is a beta, near-real-time service and does not provide a guaranteed service level.
- The API returns current warning records, so the available number of documents varies with real flood conditions.
- County filtering depends on source county text and is not a general city-name geocoder.
- The source is focused on flood warnings rather than general temperature, wind or multi-day weather forecasts.
- The first embedding run downloads the sentence-transformer model and may take longer than later runs.
- This is not a safety-critical warning application and does not replace official public warning channels.

## How the Day 2 files were repurposed

| Day 2 pattern | Homework 2 implementation |
|---|---|
| `massive_client.py` API wrapper | `weather_client.py` Environment Agency wrapper |
| `ticker_news_documents` | `weather_documents` |
| `ticker_news_embeddings` | `weather_embeddings` |
| News title/description text | Flood severity, area and warning message |
| News sync endpoint | `POST /weather/sync` |
| News vector search | `POST /weather/search` |
| SentenceTransformer singleton | Reused for weather query vectors |
| psycopg2 Lakebase helper | Reused and simplified |
| pgvector `<=>` retrieval | Reused for cosine similarity |

The stock watchlist, Massive API secret and ticker-news tables were removed from this submission repo because they are not required for Homework 2 and would create unnecessary duplication.

## Deliverable checklist

- [x] `weather_client.py`
- [x] `POST /weather/sync`
- [x] `POST /weather/search`
- [x] `weather_documents`
- [x] `weather_embeddings VECTOR(384)`
- [x] 800-character chunks with 100-character overlap
- [x] MiniLM 384-dimensional embeddings
- [x] psycopg2/`execute_values` write path
- [x] `%s::vector` cast
- [x] HNSW cosine index
- [x] stable IDs and upserts
- [x] empty-query and bounded-`top_k` handling
- [x] README with source justification, schema, run steps and limitations
