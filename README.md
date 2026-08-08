# Homework 2: NWS Weather Intelligence

This repository repurposes the Databricks Day 2 news-vector-search pattern for
a US weather use case: unstructured NWS alert and forecast text, chunked and
embedded into Lakebase Postgres with pgvector, retrieved by cosine similarity.

The end-to-end flow is:

1. **Harvest** National Weather Service (NWS) active alerts and narrative
   forecasts for a set of US locations.
2. **Normalise** each alert/forecast period into a reusable document schema.
3. **Store** raw documents and the original JSON payload in Lakebase Postgres.
4. **Chunk and embed** the narrative text with `sentence-transformers/all-MiniLM-L6-v2`.
5. **Store** 384-dimensional vectors in Lakebase using pgvector.
6. **Retrieve** the most relevant chunks through `POST /weather/search` or
   `GET /weather/search` (the GET variant also returns an LLM-generated
   summary - see [Stretch goals](#stretch-goals)).

## Why this data source was selected

The project uses the **National Weather Service API**: `https://api.weather.gov`

It was selected because it:

- is free and requires **no API key or registration** - one less secret to
  plumb through Databricks secret scopes for a homework assignment;
- returns two genuinely different shapes of free text from one source -
  alert `description`/`instruction` narrative (e.g. *"A Flash Flood Warning
  means flooding is occurring or imminent..."*) reads very differently from
  forecast `detailedForecast` narrative (e.g. *"Showers likely, mainly after
  9pm. Low around 68."*), which makes it easy to sanity-check retrieval
  quality by eye - does a flood query surface alerts, not forecasts?
- has generous, unauthenticated rate limits, so there's no risk of the kind
  of strict free-tier throttling the Day 2 ticker-news pipeline ran into
  with the Massive API;
- is straightforward to combine with a keyless geocoder (Nominatim /
  OpenStreetMap) so the API accepts plain city/state names, not just
  coordinates.

**Trade-off:** NWS only covers the United States and its territories. An
earlier draft of this project targeted the UK Environment Agency's flood
API instead (`environment.data.gov.uk`) for wider (England-only, in that
case) coverage - that direction was abandoned in favor of NWS's richer
alert+forecast text and simpler auth story, but a few files briefly
described the EA version before this cleanup pass; see
[What changed in this pass](#what-changed-in-this-pass) if you're
comparing against an older copy of this repo.

## Repository structure

```text
.
├── app.py                        # Flask app: routes only, business logic lives in weather_sync.py/llm_summary.py
├── app.yaml                      # Databricks App deployment config
├── databricks.yml                # Databricks Asset Bundle: the scheduled re-sync Job (stretch goal)
├── weather_client.py             # NWSWeatherClient: geocoding + NWS API + document normalisation
├── weather_sync.py               # Shared harvest+upsert logic (used by app.py AND the scheduled job)
├── llm_summary.py                # Optional LLM summary for GET /weather/search (stretch goal)
├── lakebase.py                   # Lakebase connection helpers + pgvector literal serialization
├── text_utils.py                 # chunk_text() - shared by the ingestion notebook and its tests
├── setup_secrets.py
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── templates/
│   └── index.html
├── jobs/
│   └── scheduled_weather_sync.py # Databricks Job entry point (stretch goal)
├── notebooks/
│   ├── ingest_weather_embeddings.ipynb
│   └── benchmark_hnsw_index.py   # HNSW vs. sequential-scan latency benchmark (stretch goal)
├── sql/
│   ├── 01_setup_weather_documents_tables.sql
│   └── 02_setup_weather_embeddings_table.sql
└── tests/
    ├── test_chunking.py
    ├── test_weather_client.py
    ├── test_weather_sync.py
    └── test_llm_summary.py
```

## API input conventions

`POST /weather/sync` accepts a `locations` list. Each entry can be:

- **US city/state string**, such as `"Chicago, IL"` or `"Austin, TX"`
- **Latitude,longitude coordinates**, such as `"41.8781,-87.6298"`

The client resolves city/state through OpenStreetMap Nominatim geocoding,
then queries NWS `/points/{lat},{lon}` to get the forecast office grid point.

**Request body:**
```json
{
  "locations": ["Chicago, IL", "Austin, TX"],
  "limit": 50
}
```

**Response:**
- `synced`: number of documents upserted (only rows whose text actually
  changed count as a write - see the schema section below)
- `unique_documents`: total unique documents collected across all locations
- `locations`: the location list that was used (falls back to
  `WEATHER_LOCATIONS` when the body omits it)
- `errors`: any locations that failed to geocode or fetch, with a reason -
  one bad location doesn't fail the whole sync

## Lakebase schema

### `weather_documents`

Raw NWS alert and forecast documents with narrative text for embedding.

| Column | Type | Purpose |
|---|---|---|
| `id` | TEXT PRIMARY KEY | `nws-alert:<nws alert id>` or `nws-forecast:<hash of location+period+startTime>` |
| `location` | TEXT NOT NULL | Resolved location label (geocoder's display name, or the raw `"lat,lon"` string) |
| `latitude` / `longitude` | DOUBLE PRECISION | Resolved coordinates |
| `source_type` | TEXT NOT NULL, `CHECK IN ('alert','forecast')` | Which NWS endpoint this came from |
| `headline` | TEXT NOT NULL | Alert headline/event, or forecast period name (e.g. "Tonight") |
| `narrative_text` | TEXT NOT NULL | Free-text body that gets chunked and embedded |
| `issued_at` / `effective_at` | TIMESTAMPTZ | When NWS issued/made this effective |
| `payload` | JSONB NOT NULL | Complete raw NWS JSON, for provenance/debugging |
| `synced_at` | TIMESTAMPTZ NOT NULL | Last time this row's content actually changed |

### `weather_embeddings`

| Column | Purpose |
|---|---|
| `id` | `sha256(document_id\|chunk_index\|model_name)` |
| `document_id` | FK to `weather_documents.id`, `ON DELETE CASCADE` |
| `chunk_index` | Position of the chunk within the document's narrative text |
| `chunk_text` | The passage actually returned by retrieval |
| `content_hash` | SHA-256 of the source narrative_text - lets the ingestion notebook skip re-embedding documents whose text hasn't changed |
| `embedding` | `VECTOR(384)` |
| `model_name` | Which sentence-transformers model produced this vector |
| `created_at` | Embedding write time |

`UNIQUE (document_id, chunk_index, model_name)` + `ON CONFLICT ... DO UPDATE`
makes re-running the ingestion notebook idempotent, and stale chunks are
explicitly deleted before re-inserting so a shortened re-issued warning
doesn't leave orphaned old chunks behind.

The ingestion notebook uses an 800-character chunk size and 100-character
overlap (`text_utils.chunk_text`, shared with `tests/test_chunking.py`).
Most individual NWS narratives (one forecast period, or one alert's
description+instruction) are well under 800 characters, so most documents
produce exactly one chunk - chunking mainly matters for longer combined
alert text.

## Stretch goals

| Goal | Status | Where |
|---|---|---|
| `GET /weather/search?query=...` with an LLM-generated summary (basic RAG) | **Done** | `llm_summary.py`, wired into `app.py`'s new `GET /weather/search` route (also available on `POST` via `"summarize": true`) |
| Dedupe/upsert on `id` so re-syncing doesn't duplicate rows | **Done** | `weather_sync.py`'s `ON CONFLICT` upsert + the notebook's content-hash change detection and stale-chunk cleanup |
| Scheduled Databricks Job that re-syncs on an interval | **Done** | `databricks.yml` (`weather_resync_job`, every 15 min, paused by default) + `jobs/scheduled_weather_sync.py` |
| Combine two data sources (e.g. alerts + forecast discussions), filter by `source_type` | **Done** | Alerts and forecast periods are two distinct NWS endpoints, combined into one `weather_documents` table; `source_type` is filterable on both search routes |
| Benchmark: HNSW index query latency, with vs. without the index | **Done** | `notebooks/benchmark_hnsw_index.py` |

Details on each:

**LLM summary.** `GET /weather/search?query=...&summarize=true` (the
default) calls a Databricks Foundation Model APIs chat endpoint via
`WorkspaceClient().serving_endpoints.query()` - the same auth path
`lakebase.py` already uses for secrets, no new credential needed. It fails
soft: if `SUMMARY_MODEL_ENDPOINT` is unset, the workspace doesn't have
Foundation Model APIs enabled, or the call errors for any reason,
`summary` comes back `null` and the vector-search `results` are returned
unaffected. **You do need to point `SUMMARY_MODEL_ENDPOINT` at a real
endpoint your workspace has** (check Compute -> Serving) and make sure the
app's service principal has "Can Query" on it - see `.env.example`.

**Scheduled job.** `databricks bundle deploy` creates `weather_resync_job`
with two chained tasks: `sync_documents` (runs
`jobs/scheduled_weather_sync.py`, which calls the same
`weather_sync.run_weather_sync()` the Flask route calls) then
`embed_new_documents` (runs the ingestion notebook). It's **paused by
default** - flip `schedule.pause_status` in `databricks.yml` or run
`databricks bundle run weather_resync_job` once you've confirmed it works,
so a fresh deploy doesn't immediately start burning NWS/geocoder/embedding
calls every 15 minutes.

**HNSW benchmark.** `python notebooks/benchmark_hnsw_index.py` times the
same `ORDER BY embedding <=> query LIMIT k` pattern `/weather/search` uses,
first with the HNSW index, then with it temporarily dropped (forcing a
sequential scan), using Postgres's own `EXPLAIN ANALYZE` timing rather than
Python wall-clock time. It always restores the index afterward. **Honest
caveat the script prints itself:** HNSW's advantage mainly shows up at
thousands of rows; on a small homework dataset the two numbers may be close
or even favor the sequential scan slightly.

## How the Day 2 files were repurposed

| Day 2 pattern | Homework 2 implementation |
|---|---|
| `massive_client.py` API wrapper | `weather_client.py` NWS API wrapper |
| `ticker_news_documents` | `weather_documents` |
| `ticker_news_embeddings` | `weather_embeddings` |
| News title/description text | Alert headline/description+instruction, forecast `detailedForecast` |
| News sync endpoint | `POST /weather/sync` (+ scheduled via `weather_sync.py`/`databricks.yml`) |
| News vector search | `POST /weather/search` + `GET /weather/search` with summary |
| SentenceTransformer singleton | Reused for both document and query embeddings |
| psycopg2 Lakebase helper | Reused, extended with `vector_literal()` |
| pgvector `<=>` retrieval | Reused for cosine similarity, plus `source_type` filtering |
| Chunking logic | Reused 800/100 parameters, centralized in `text_utils.py` |

The stock watchlist, Massive API secret, and ticker-news tables were
removed from this submission repo since they're not needed for Homework 2.

## Clear homework steps

### Step 1: Prepare the project

Upload or clone this entire repository into a Databricks Git folder. Keep
the folder structure unchanged - `weather_sync.py`, the notebook, and
`jobs/scheduled_weather_sync.py` all assume `lakebase.py`/`weather_client.py`
/`text_utils.py` are importable from the repository root.

```bash
pip install -r requirements.txt
```

### Step 2: Create Lakebase

1. Open **Lakebase** in the Databricks workspace.
2. Create a database instance.
3. Create a native Postgres role with password authentication.
4. Copy the connection URL - it looks like:

```text
postgresql://<role>:<password>@<host>:5432/databricks_postgres?sslmode=require
```

### Step 3: Store the Lakebase URL as a secret

```bash
python setup_secrets.py
```

Stores the URL as `database/lakebase-url`. The NWS API itself needs no
secret or API key.

### Step 4: Create the database tables (both files, in order - manual)

There is **no code path that auto-creates these tables** - `app.py` and the
notebook both assume they already exist. Open a Postgres SQL editor
connected to Lakebase and run, **in order**:

1. `sql/01_setup_weather_documents_tables.sql` - creates `weather_documents`.
2. `sql/02_setup_weather_embeddings_table.sql` - enables the `vector`
   extension, creates `weather_embeddings` with `VECTOR(384)`, and creates
   the HNSW cosine index.

### Step 5: Run the Flask app

```bash
cp .env.example .env   # fill in the real Lakebase URL
python app.py
```

Starts on port `8000` by default.

```bash
curl http://localhost:8000/healthz
```

### Step 6: Harvest NWS weather documents

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["Chicago, IL", "Austin, TX", "Seattle, WA"], "limit": 50}'
```

By coordinates:

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations": ["41.8781,-87.6298"], "limit": 50}'
```

Check what was harvested:

```bash
curl "http://localhost:8000/weather/documents?limit=20"
```

```sql
SELECT id, location, source_type, headline, synced_at
FROM weather_documents
ORDER BY synced_at DESC;
```

### Step 7: Chunk and create embeddings

```bash
# in a Databricks notebook, or locally with LAKEBASE_URL set:
notebooks/ingest_weather_embeddings.ipynb
```

The notebook:

1. reads `weather_documents` with psycopg2;
2. detects new/changed narrative text via SHA-256 hashes;
3. chunks changed text with `text_utils.chunk_text` (800/100);
4. loads `sentence-transformers/all-MiniLM-L6-v2` once;
5. creates 384-dimensional vectors;
6. deletes stale chunks for changed documents, then upserts new ones via
   `execute_values` with an explicit `%s::vector` cast;
7. runs a validation query against `weather_embeddings` using the exact
   same `<=>` pattern `/weather/search` uses.

Verify:

```sql
SELECT COUNT(*) AS embedding_rows, COUNT(DISTINCT document_id) AS embedded_documents
FROM weather_embeddings;
```

### Step 8: Run semantic retrieval

Vector search only:

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "flooding may affect roads and nearby properties", "top_k": 5}'
```

With an AI-generated summary (GET variant, the stretch goal):

```bash
curl "http://localhost:8000/weather/search?query=flash+flood+risk+near+rivers+this+weekend&top_k=5"
```

Filter by source type:

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query": "immediate action required", "top_k": 5, "source_type": "alert"}'
```

### Step 9: Deploy as a Databricks App + schedule the resync Job

1. Go to **Compute -> Apps**, create a custom app, select this Git folder,
   confirm `app.yaml` is detected, deploy.
2. Grant the app's service principal read access to `database/lakebase-url`
   if your workspace requires explicit permission, and "Can Query" on
   `SUMMARY_MODEL_ENDPOINT` if you want the summary feature.
3. `databricks bundle deploy` to create the scheduled `weather_resync_job`
   (see [Stretch goals](#stretch-goals) - it's paused by default).

### Step 10: Demonstrate the homework

1. Show an empty (or existing) `weather_documents` table.
2. Call `POST /weather/sync`.
3. Query `weather_documents` for raw text + JSON payloads.
4. Run `ingest_weather_embeddings.ipynb`.
5. Query `weather_embeddings` for vector rows.
6. Call `GET /weather/search` with a natural-language risk query and show
   the AI summary alongside the raw ranked chunks.
7. Optionally: run `notebooks/benchmark_hnsw_index.py` and `databricks
   bundle run weather_resync_job` to show the two remaining stretch goals
   working live.

### Step 11: Run the checks

```bash
pip install -r requirements-dev.txt
python -m compileall .
pytest -q
```

## Example search response (GET, with summary)

```json
{
  "query": "flash flood warning near rivers this weekend",
  "top_k": 5,
  "count": 2,
  "summary": "Chicago, IL is currently under a Flash Flood Warning - flooding is occurring or imminent and residents should move to higher ground. Austin, TX has no active alert; its forecast calls for showers and thunderstorms that could bring heavy rainfall this weekend.",
  "results": [
    {
      "id": "nws-alert:urn:oid:2.49.0.1.840.0.abc123",
      "location": "Chicago, IL",
      "latitude": 41.8781,
      "longitude": -87.6298,
      "source_type": "alert",
      "headline": "Flash Flood Warning",
      "chunk_index": 0,
      "chunk_text": "A Flash Flood Warning means flooding is occurring or imminent. Move to higher ground immediately...",
      "similarity": 0.82
    },
    {
      "id": "nws-forecast:9f3a1c2b8e7d4560",
      "location": "Austin, TX",
      "latitude": 30.2672,
      "longitude": -97.7431,
      "source_type": "forecast",
      "headline": "Saturday Night",
      "chunk_index": 0,
      "chunk_text": "Showers and thunderstorms likely. Some storms may produce heavy rainfall...",
      "similarity": 0.71
    }
  ],
  "message": null
}
```

## Known limitations

- The NWS API covers the United States and its territories only.
- Geocoding depends on Nominatim/OpenStreetMap availability and its own
  fair-use rate limits (self-throttled to ~1 request/sec in `weather_client.py`).
- Active alerts vary with real weather - may be sparse during calm weather.
- `SUMMARY_MODEL_ENDPOINT` must point at an endpoint your workspace
  actually has enabled; there's no universal default name across all
  Databricks workspaces/regions.
- The first embedding run downloads the sentence-transformer model
  (~100MB) and may take longer than subsequent runs.
- The scheduled Job's `sync_documents` task and the ingestion notebook's
  `embed_new_documents` task both need network egress to `api.weather.gov`
  and (for city/state input) `nominatim.openstreetmap.org`.
- This is a homework/demo application, not an official NWS warning channel.

## What changed in this pass

An earlier version of this repository had drifted: the actual code
(`weather_client.py`, `app.py`, `templates/index.html`, both SQL files, the
notebook) implemented NWS, but `README.md`, `HOMEWORK_STEPS.md`,
`.env.example`, and `tests/test_weather_client.py` still described an
abandoned UK Environment Agency flood-API design - including a test file
that imported a class (`EnvironmentAgencyWeatherClient`) that didn't exist
in `weather_client.py`, which made `pytest` fail at collection. This pass:

- Rewrote `README.md`, `HOMEWORK_STEPS.md`, and `.env.example` to describe
  what the code actually does.
- Rewrote `tests/test_weather_client.py` against the real `NWSWeatherClient`,
  and added `tests/test_weather_sync.py`/`tests/test_llm_summary.py` for
  the new modules - all offline, no live network/DB calls.
- Fixed two real bugs found in `notebooks/ingest_weather_embeddings.ipynb`:
  its setup cell uninstalled `psycopg2`/`psycopg2-binary` and never
  reinstalled `psycopg2-binary`, which would have broken `import lakebase`;
  and a later cell read a `validation_top_k` widget that was never created,
  which would have raised `InputWidgetNotDefined`. Also dropped an unused
  `trafilatura` install (leftover from the Day 2 template's HTML-scraping
  step, which this JSON-only pipeline never needed).
- Removed a duplicated `chunk_text()`/`vector_literal()` implementation
  from the notebook in favor of importing `text_utils.chunk_text` and the
  new `lakebase.vector_literal()`, so there's one implementation of each
  instead of two that could silently drift apart.
- Extracted the sync harvest+upsert logic out of `app.py` into
  `weather_sync.py` so the new scheduled job doesn't duplicate it.
- Removed a dead `item.source_url` branch from `templates/index.html` (no
  API response has ever included that field).
- Implemented all five stretch goals (see above).

## Deliverable checklist

- [x] `weather_client.py`
- [x] `POST /weather/sync` (+ scheduled via `weather_sync.py`/`databricks.yml`)
- [x] `POST /weather/search` and `GET /weather/search`
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
- [x] All 5 stretch goals