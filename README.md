# Homework 2: NWS Weather Intelligence

This project adapts the Databricks Day 2 Lakebase application pattern from a
stock watchlist and news search experience to weather ingestion and semantic
retrieval.

## User interface

The Flask app has two tabs.

### 1. My Locations

The user searches for a US location in `City, ST` format, for example:

```text
New York, NY
Chicago, IL
Austin, TX
```

The suggestions come from the application's built-in catalogue of US state
capitals and major cities. Selecting one of these suggestions avoids a public
geocoder request.

Submitting the form calls the required endpoint:

```http
POST /weather/sync
```

After the sync, the location appears in a table showing:

- alert count;
- forecast count;
- total stored weather documents;
- latest sync time.

The list is derived from `weather_documents`, so no additional watchlist table
is required and the locations remain visible after a page refresh.

### 2. Vector Search

The user enters a natural-language question, such as:

```text
flash flood risk this weekend
```

The interface calls the required endpoint:

```http
POST /weather/search
```

The user can optionally filter by:

- stored location;
- `alert` or `forecast`;
- number of results from 1 to 20.

The results show the location, source type, headline, effective time, retrieved
chunk and cosine-similarity score.

The interface intentionally says **weather alerts and forecasts**, not
**weather news**. The current ingestion pipeline does not load an external news
source. Adding weather news would be a separate multi-source stretch goal.

## Homework 2 requirement mapping

| Homework requirement | Implementation |
|---|---|
| Harvest unstructured weather text | `weather_client.py` |
| Resolve locations and fetch NWS content | `NWSWeatherClient` |
| Normalised raw-document table | `weather_documents` |
| Required sync endpoint | `POST /weather/sync` |
| Chunk at 800 with overlap 100 | `text_utils.py` and ingestion notebook |
| MiniLM 384-dimensional vectors | `all-MiniLM-L6-v2` |
| psycopg2 vector writes | embedding notebook with `%s::vector` |
| pgvector HNSW index | `sql/02_setup_weather_embeddings_table.sql` |
| Required retrieval endpoint | `POST /weather/search` |
| Query model loaded once | singleton in `app.py` |
| Missing query and `top_k` bounds | handled in `app.py` |
| Empty embedding table | explanatory empty-state response |
| Data-source and schema documentation | this README |

The new UI endpoints are additive and do not change the required request
contracts.

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/healthz` | Health check |
| `GET` | `/weather/location-options` | Supported city/state suggestions |
| `GET` | `/weather/locations` | Stored-location summary for the first tab |
| `POST` | `/weather/sync` | Required weather ingestion endpoint |
| `GET` | `/weather/documents` | Inspect raw weather documents |
| `POST` | `/weather/search` | Required vector-search endpoint |
| `GET` | `/weather/search` | Optional browser-friendly variant |

## Required pipeline

### 1. Create the tables

Run:

```text
sql/01_setup_weather_documents_tables.sql
sql/02_setup_weather_embeddings_table.sql
```

### 2. Sync weather text

Through the UI or directly:

```bash
curl -X POST http://localhost:8000/weather/sync \
  -H "Content-Type: application/json" \
  -d '{"locations":["New York, NY"],"limit":50}'
```

### 3. Generate embeddings

Run:

```text
notebooks/ingest_weather_embeddings.ipynb
```

The notebook reads `weather_documents`, chunks changed text, creates
384-dimensional MiniLM vectors and writes them to `weather_embeddings` with
psycopg2.

### 4. Search

```bash
curl -X POST http://localhost:8000/weather/search \
  -H "Content-Type: application/json" \
  -d '{"query":"flash flood risk this weekend","top_k":5}'
```

Optional filters used by the interface:

```json
{
  "query": "strong winds and travel disruption",
  "top_k": 5,
  "source_type": "alert",
  "location": "New York, NY",
  "summarize": false
}
```

## Important terminology

This application retrieves **weather documents**, meaning NWS alert and
forecast text. It does not retrieve general media news articles.

## Known limitations

- NWS covers the United States and its territories.
- The built-in city suggestions cover state capitals and major cities, not
  every US settlement.
- A city outside the built-in catalogue may fall back to Nominatim, which can
  block shared cloud IP addresses.
- Newly synced documents are searchable only after the embedding notebook is
  run.
- The optional AI summary requires `SUMMARY_MODEL_ENDPOINT`; it is disabled by
  default.
