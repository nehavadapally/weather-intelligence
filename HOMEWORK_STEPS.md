# Homework 2 execution checklist

1. Upload or clone the repository into a Databricks Git folder.
2. Create a Lakebase instance and a native PostgreSQL role.
3. Store the Lakebase URL in the `database/lakebase-url` secret.
4. Run the SQL files in order:
   - `sql/01_setup_weather_documents_tables.sql`
   - `sql/02_setup_weather_embeddings_table.sql`
5. Start or deploy `app.py`.
6. Confirm `GET /healthz` returns `{"status":"ok"}`.
7. Call `POST /weather/sync` with valid US locations or coordinates.
8. Confirm rows exist in `weather_documents`.
9. Run `notebooks/ingest_weather_embeddings.ipynb`.
10. Confirm rows exist in `weather_embeddings`.
11. Call `POST /weather/search` with a natural-language query.
12. Capture the sync response, SQL row counts, notebook result and search
    response for the submission.

Example sync body:

```json
{
  "locations": ["Chicago, IL", "Austin, TX"],
  "limit": 50
}
```

Example search body:

```json
{
  "query": "flash flood risk near rivers",
  "top_k": 5
}
```

The notebook uses the Databricks runtime-provided `psycopg2` package. It must
not uninstall that package.
