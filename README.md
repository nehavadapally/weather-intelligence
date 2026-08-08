# Homework 2: Weather Intelligence with Databricks Lakebase

## 1. Project Overview

This project uses **Databricks Lakebase** and **pgvector** to store and search weather alerts and forecasts.

App URL: https://weather-app-7474645166709307.aws.databricksapps.com/

### Pipeline

```text
National Weather Service API
            ↓
Databricks App
POST /weather/sync
            ↓
Lakebase: weather_documents
            ↓
Databricks Embedding Notebook
            ↓
Lakebase: weather_embeddings
            ↓
Databricks App
POST /weather/search
            ↓
Search Results
```

The application has two main tabs:

* **My Locations** – Add, sync, refresh, and delete US locations.
* **Vector Search** – Search weather alerts and forecasts using natural-language questions.

Screenshots and other submission evidence are stored in:

```text
evidence/
```

---

# 2. Data Source

The project uses the **US National Weather Service (NWS) API**.

The NWS API was selected because it:

* Is public.
* Does not require an API key.
* Provides active weather alerts.
* Provides detailed forecasts.
* Provides text suitable for embeddings.
* Provides timestamps and location information.

The application collects:

* Weather alerts.
* Alert descriptions and instructions.
* Forecast information.
* Forecast periods and timestamps.

> The NWS API only covers the United States and its territories.

---

# 3. Databricks Architecture

The project uses these Databricks components:

| Component             | Purpose                                            |
| --------------------- | -------------------------------------------------- |
| Databricks Git Folder | Stores application, SQL, notebook, and other files |
| Lakebase              | Stores weather documents and embeddings            |
| Databricks Secrets    | Stores the Lakebase connection URL                 |
| Databricks Notebook   | Creates text chunks and embeddings                 |
| Databricks App        | Provides the UI and REST API                       |
| Model Serving         | Optional AI summary of search results              |

---

# 4. Database Tables

The project uses two Lakebase tables.

## `weather_documents`

Stores the original weather information.

| Column           | Purpose                         |
| ---------------- | ------------------------------- |
| `id`             | Unique document ID              |
| `location`       | Location such as `New York, NY` |
| `latitude`       | Location latitude               |
| `longitude`      | Location longitude              |
| `source_type`    | `alert` or `forecast`           |
| `headline`       | Alert or forecast title         |
| `narrative_text` | Main weather text               |
| `issued_at`      | Issue time                      |
| `effective_at`   | Effective time                  |
| `payload`        | Original NWS JSON data          |
| `synced_at`      | Last sync time                  |

The application uses stable IDs and `ON CONFLICT` upserts to prevent duplicate records.

## `weather_embeddings`

Stores text chunks and their vector embeddings.

| Column         | Purpose                  |
| -------------- | ------------------------ |
| `id`           | Unique embedding ID      |
| `document_id`  | Related weather document |
| `chunk_index`  | Chunk position           |
| `chunk_text`   | Text used for search     |
| `content_hash` | Detects changed text     |
| `embedding`    | 384-dimensional vector   |
| `model_name`   | Embedding model          |
| `created_at`   | Creation time            |

The relationship uses:

```sql
ON DELETE CASCADE
```

Therefore, deleting a weather document also deletes its embeddings.

---

# 5. Embedding Configuration

The embedding notebook uses:

| Setting          | Value                                    |
| ---------------- | ---------------------------------------- |
| Chunk size       | 800 characters                           |
| Chunk overlap    | 100 characters                           |
| Model            | `sentence-transformers/all-MiniLM-L6-v2` |
| Vector size      | 384                                      |
| Database library | `psycopg2`                               |
| Batch insert     | `execute_values`                         |
| Vector index     | HNSW                                     |
| Distance         | Cosine distance                          |

The same embedding model is used for:

* Weather documents.
* User search queries.

---

# 6. Project Files

The main project structure is:

```text
weather-intelligence/
├── app.py
├── app.yaml
├── lakebase.py
├── weather_client.py
├── weather_sync.py
├── llm_summary.py
├── text_utils.py
├── notebooks/
│   └── ingest_weather_embeddings.ipynb
├── sql/
│   ├── 01_setup_weather_documents_tables.sql
│   └── 02_setup_weather_embeddings_table.sql
├── templates/
│   └── index.html
└── evidence/
```

---

# 7. Run the Project in Databricks

## Step 1: Open the Git Repository

Open **Workspace → Git Folders** in Databricks and clone the project repository.

```text
https://github.com/nehavadapally/weather-intelligence.git
```

---

## Step 2: Create Lakebase

Create a Databricks Lakebase PostgreSQL database.

The connection URL has this format:

```text
postgresql://username:password@host:5432/database?sslmode=require
```

Do not commit this connection string or password to GitHub.

---

## Step 3: Configure Databricks Secrets

Create a Databricks secret:

```text
Scope: database
Key: lakebase-url
```

The application uses:

```text
LAKEBASE_SECRET_SCOPE=database
LAKEBASE_SECRET_KEY=lakebase-url
```

For local testing, the connection can also be provided using:

```text
LAKEBASE_URL
```

---

## Step 4: Create the Lakebase Tables

Run the SQL files in this order:

```text
sql/01_setup_weather_documents_tables.sql
sql/02_setup_weather_embeddings_table.sql
```

The second file:

* Enables pgvector.
* Creates `weather_embeddings`.
* Creates `VECTOR(384)`.
* Creates the HNSW vector index.

Check that the tables exist:

```sql
SELECT table_name
FROM information_schema.tables
WHERE table_name IN (
    'weather_documents',
    'weather_embeddings'
);
```

---

# 8. Deploy the Databricks App

Create a **Databricks App** using the project Git folder.

The application is configured through:

```text
app.yaml
```

The application starts with:

```text
python app.py
```

The main API endpoints are:

```text
GET  /healthz
POST /weather/sync
POST /weather/search
```

After deployment, open the Databricks App URL.

Check:

```text
https://<databricks-app-url>/healthz
```

Expected response:

```json
{
  "status": "ok"
}
```

---

# 9. Sync Weather Data

Open the **My Locations** tab.

Enter a US location using:

```text
City, ST
```

Example:

```text
New York, NY
```

Click **Add and Sync Location**.

The application calls:

```text
POST /weather/sync
```

Example request:

```json
{
  "locations": ["New York, NY"],
  "limit": 50
}
```

The sync process:

1. Finds the location coordinates.
2. Calls the NWS API.
3. Gets alerts and forecasts.
4. Converts the data into a standard format.
5. Saves the data in `weather_documents`.

Check the stored data:

```sql
SELECT
    id,
    location,
    source_type,
    headline,
    synced_at
FROM weather_documents
ORDER BY synced_at DESC;
```

---

# 10. Create Embeddings

Open:

```text
notebooks/ingest_weather_embeddings.ipynb
```

Run the notebook from top to bottom.

The notebook:

1. Connects to Lakebase.
2. Reads `weather_documents`.
3. Finds new or changed documents.
4. Splits text into 800-character chunks.
5. Uses 100-character overlap.
6. Creates embeddings using MiniLM.
7. Stores the vectors in `weather_embeddings`.
8. Removes old chunks when source text changes.

The notebook uses `psycopg2`.

Do not uninstall the Databricks-provided `psycopg2`.

Check the embedding data:

```sql
SELECT
    COUNT(*) AS embedding_rows,
    COUNT(DISTINCT document_id) AS embedded_documents
FROM weather_embeddings;
```

---

# 11. Vector Search

Open the **Vector Search** tab.

Enter a question such as:

```text
risk of flooding near rivers
```

The application calls:

```text
POST /weather/search
```

Example request:

```json
{
  "query": "risk of flooding near rivers",
  "top_k": 5
}
```

The search process:

1. Creates an embedding for the question.
2. Searches the stored weather vectors.
3. Calculates cosine similarity.
4. Returns the most relevant weather passages.

`top_k` is limited to 1–20 results.

### Optional Filters

Search can also be filtered by:

* Location.
* Alert or forecast.
* Number of results.

Example:

```json
{
  "query": "strong winds and travel disruption",
  "top_k": 5,
  "source_type": "alert",
  "location": "New York, NY",
  "summarize": false
}
```

---

# 12. Refresh and Delete Locations

## Refresh

Click **Sync** for an existing location.

This updates the weather documents.

After syncing new or changed documents, run the embedding notebook again so the new information becomes searchable.

## Delete

Click **Delete** for a location.

The deletion works like this:

```text
Delete weather documents
          ↓
ON DELETE CASCADE
          ↓
Delete related embeddings
          ↓
Remove location from the UI
```

---

# 13. Optional AI Summary

The application can optionally generate an AI summary of search results using **Databricks Model Serving**.

To enable it, configure the serving endpoint in `app.yaml`:

```yaml
- name: SUMMARY_MODEL_ENDPOINT
  value: "your-serving-endpoint-name"
```

The Databricks App must have permission to query the Model Serving endpoint.

If Model Serving is unavailable, normal vector-search results still work.

---

# 14. Known Limitations

* NWS data is limited to the US and its territories.
* The built-in location list does not contain every US location.
* Some unknown locations may use Nominatim geocoding.
* Newly synced documents must be embedded before they can be searched.
* Active alert counts change with current weather conditions.
* The first embedding run may take longer because the model needs to download.
* AI summaries require a working Model Serving endpoint.
* This project is for learning and should not replace official emergency warnings.

---

# 15. Future Improvements

Possible improvements include:

* Schedule weather sync using a Databricks Job.
* Automatically run embeddings after syncing.
* Create a workflow:

```text
Sync Weather
     ↓
Create Embeddings
     ↓
Validate Data
     ↓
Notify on Failure
```

Other improvements could include:

* Hourly forecasts.
* Additional weather data sources.
* Authentication.
* Per-user saved locations.
* Recency-based search ranking.
* Monitoring and dashboards.
* Automated API tests.
* Performance comparison between HNSW and exact vector search.
---
