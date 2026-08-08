# Homework 2 execution checklist

The full explanation of each step lives in `README.md`; this is the
condensed version for a grading run-through.

1. Upload the repository to a Databricks Git folder (keep the folder
   structure - `weather_sync.py`/the notebook/`jobs/` all import
   `lakebase.py`, `weather_client.py`, and `text_utils.py` from the repo root).
2. `pip install -r requirements.txt`.
3. Create a Lakebase instance and a native-password Postgres role.
4. `python setup_secrets.py` to store `database/lakebase-url`.
5. Run **both** SQL files manually, in order - there is no code path that
   creates these tables for you:
   - `sql/01_setup_weather_documents_tables.sql`
   - `sql/02_setup_weather_embeddings_table.sql`
6. Start or deploy `app.py`.
7. Call `POST /weather/sync` with real US locations, e.g.
   `{"locations": ["Chicago, IL", "Austin, TX", "Seattle, WA"], "limit": 50}`.
   (NWS only covers the US - a location like `"all"` will fail geocoding.)
8. Confirm rows exist in `weather_documents`.
9. Run `notebooks/ingest_weather_embeddings.ipynb`.
10. Confirm rows exist in `weather_embeddings`.
11. Call `GET /weather/search?query=...` with a natural-language query - the
    default response includes an AI-generated summary in addition to the
    ranked chunks (pass `&summarize=false` to skip it and just get raw
    vector-search results).
12. Capture the API response, relevant SQL evidence, and a short reflection
    for submission.