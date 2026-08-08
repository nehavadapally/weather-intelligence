# Homework 2 execution checklist

1. Upload the repository to a Databricks Git folder.
2. Install `requirements.txt`.
3. Create Lakebase and a native-password Postgres role.
4. Run `python setup_secrets.py` to store `database/lakebase-url`.
5. Run `sql/01_setup_weather_tables.sql` in Lakebase.
6. Start or deploy `app.py`.
7. Call `POST /weather/sync` with `{"locations":["all"],"min_severity":3}`.
8. Confirm rows exist in `weather_documents`.
9. Run `python notebooks/ingest_weather_embeddings.py`.
10. Confirm rows exist in `weather_embeddings`.
11. Call `POST /weather/search` with a natural-language flood-risk query.
12. Capture the API response, relevant SQL evidence and a short reflection for submission.

For a wider demo sample, use `min_severity: 4` because live active-warning volumes vary.
