# API Usage

DataSight exposes a versioned FastAPI API under `/api/v1`. Interactive OpenAPI documentation is available at `/docs` while the API service is running.

## Run The API

```powershell
docker compose up -d postgres redis grobid
docker compose run --rm migrate
docker compose up api worker
```

Host development:

```powershell
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn datasight.interfaces.api.main:app --reload --host 0.0.0.0 --port 8000
```

Base URL:

```text
http://localhost:8000/api/v1
```

## Main Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Check database, Redis, worker, GROBID, and migration readiness. |
| `GET` | `/stages` | Return the canonical pipeline stages and descriptions. |
| `POST` | `/runs` | Enqueue a full pipeline run. |
| `POST` | `/stages/{stage}/runs` | Enqueue one stage. |
| `GET` | `/runs` | List recent pipeline runs. |
| `GET` | `/runs/{run_id}` | Read detailed run progress. |
| `GET` | `/runs/{run_id}/events` | Read chronological run events. |
| `GET` | `/runs/{run_id}/discovery-candidates` | Page through candidates retained for a preview-backed run. |
| `GET` | `/runs/{run_id}/candidates` | Page through one summary row per selected, PDF-ready paper, including stage pass flags. |
| `GET` | `/runs/{run_id}/candidates/export.csv` | Export all selected paper summaries, optionally selecting columns. |
| `GET` | `/runs/{run_id}/insights` | Page through detailed result rows, including selected papers with no detected mention. |
| `GET` | `/runs/{run_id}/insights/export.csv` | Export all detailed result rows, optionally selecting columns. |
| `GET` | `/insights` | Preview joined insight rows and the canonical ordered column list. |
| `GET` | `/insights/export.csv` | Download every insight row as CSV, optionally selecting columns. |
| `GET` | `/discovery/um-profile` | Inspect UM catalog coverage and estimated funnel phases. |
| `POST` | `/discovery/preview` | Run a cost-bounded adaptive funnel, seeded random sample, or advanced manual preview. |
| `GET` | `/openalex/status` | Check API-key readiness and safe budget metadata. |
| `POST` | `/um-datasets/import` | Import UM dataset metadata from a local CSV or JSON file. |
| `GET` | `/um-datasets` | Search and paginate stored UM dataset metadata. |
| `GET` | `/um-datasets/verification` | Compare stored metadata with the configured authoritative UM export. |
| `GET` | `/um-datasets/{um_dataset_id}` | Inspect one normalized UM dataset record and its raw metadata. |
| `POST` | `/admin/reset` | Clear database rows and generated storage files. |

## Discovery Preview And Full Run

```powershell
curl -X POST http://localhost:8000/api/v1/discovery/preview `
  -H "Content-Type: application/json" `
  -d "{\"strategy_version\":2,\"mode\":\"catalog_funnel\",\"focus_query\":\"\",\"manual_query\":null,\"from_year\":2020,\"to_year\":2026,\"publication_types\":[\"article\",\"preprint\"],\"discovery_limit\":500,\"processing_limit\":50,\"max_cost_usd\":0.25}"
```

Review the returned candidates, then launch using the unexpired preview:

```powershell
curl -X POST http://localhost:8000/api/v1/runs `
  -H "Content-Type: application/json" `
  -d "{\"preview_id\":\"PREVIEW_UUID\",\"processing_limit\":40,\"excluded_candidate_ids\":[\"W123\"],\"overwrite\":false,\"output_path\":\"storage/exports/insights.csv\",\"strategy\":\"standard\"}"
```

The processing override may only reduce the preview's target. Set `"strategy\":\"high_throughput\"` to stream selected papers through run-scoped queues. Query-first full runs and lane configuration are intentionally rejected. Random discovery uses `mode="random"` and OpenAlex's native sample operation; an optional `random_seed` makes the draw reproducible, while an omitted seed is generated and returned with the preview. Advanced manual discovery uses `mode="manual"` plus `manual_query`. All three modes use the same preview, review, expiry, cost-ceiling, and run-isolation workflow.

Seeded random-sample example:

```powershell
curl -X POST http://localhost:8000/api/v1/discovery/preview `
  -H "Content-Type: application/json" `
  -d "{\"strategy_version\":2,\"mode\":\"random\",\"focus_query\":\"\",\"manual_query\":null,\"random_seed\":42,\"from_year\":2020,\"to_year\":2026,\"publication_types\":[\"article\",\"preprint\"],\"discovery_limit\":500,\"processing_limit\":50,\"max_cost_usd\":0.25}"
```

Response:

```json
{
  "pipeline_run_id": 1,
  "task_id": "celery-task-id",
  "status": "queued"
}
```

Poll progress:

```powershell
curl http://localhost:8000/api/v1/runs/1
curl http://localhost:8000/api/v1/runs/1/events
```

## Insight CSV Export

Run-specific candidates and insights use a friendly `discovery_method` label.
Candidate summaries contain `pdf_downloaded`, `grobid_converted`, and
`document_rendered` flags. Detailed insight rows omit these flags and contain
empty insight fields when a selected paper has no detected mention. Text cells
that could be interpreted as spreadsheet formulas are prefixed with an apostrophe
in CSV exports.

`GET /insights` returns `columns` in canonical display/export order and a limited `rows` preview. Each row includes `discovery_mode` and the ordered `discovery_methods` evidence routes. Historical rows without persisted discovery provenance use `unrecorded` and an empty method list.

Omit `columns` to export every column, or repeat it to select a subset. The server restores canonical column order and rejects unknown or empty selections:

```powershell
curl "http://localhost:8000/api/v1/insights/export.csv?columns=paper_id&columns=discovery_mode&columns=discovery_methods" -OutFile datasight-insights.csv
```

## Single Stage Run

The direct `discover` stage is retained as a development diagnostic. It is not the production full-run entrypoint and does not provide preview isolation.

```powershell
curl -X POST http://localhost:8000/api/v1/stages/discover/runs `
  -H "Content-Type: application/json" `
  -d "{\"query\":\"Maastricht dataset reuse\",\"limit\":10}"
```

Valid stages:

```text
discover
download_pdf
grobid_convert
render_document
detect_mentions
extract_features
match_um_dataset
export_insights
```

## Import UM Dataset Metadata

Matching requires UM dataset records before `match_um_dataset` runs. The importer supports either the original compact CSV/JSON shape or the authoritative `data/um_dataset` directory. Directory exports may be comma- or tab-delimited and UTF-8 or Windows-1252. Both `OPEN_ALEX_AFFILIATION.csv` and the older plural filename are accepted. MeSH exports are deliberately ignored because OpenAlex does not expose a stable, supported MeSH contract. A validated directory import synchronizes the catalog; compact CSV/JSON imports remain additive.

The GUI's **UM datasets** page uses the catalog endpoints to search stored records and compares every normalized field and raw payload against `UM_DATASETS_PATH`. A mismatch reports missing, unexpected, and changed records without modifying storage. When the database is empty, the page offers a one-time import action for the configured authoritative catalog; the import endpoint and CLI remain available for explicit repairs.

Compact CSV files should include at least:

```text
um_dataset_id,title
```

Optional list-like fields such as `aliases`, `creators`, and `keywords` use semicolons.

```powershell
curl -X POST http://localhost:8000/api/v1/um-datasets/import `
  -H "Content-Type: application/json" `
  -d "{\"path\":\"data/um_dataset\"}"
```

PURE export directory example:

```powershell
curl -X POST http://localhost:8000/api/v1/um-datasets/import `
  -H "Content-Type: application/json" `
  -d "{\"path\":\"data/um_dataset\"}"
```

## Reset

Reset is intentionally guarded and requires the exact confirmation string:

```powershell
curl -X POST http://localhost:8000/api/v1/admin/reset `
  -H "Content-Type: application/json" `
  -d "{\"confirm\":\"RESET DATASIGHT\",\"force\":true}"
```

`force=true` is required when active runs exist. Reset truncates pipeline-generated application tables, restarts their identity sequences, and removes configured `storage/` artifacts. It preserves the imported `um_datasets` catalog, the Alembic migration marker, source code, migrations, `.env`, Docker volumes, and curated inputs under `data/`.

## GUI Polling Flow

The frontend uses the same API:

1. `GET /health`, `/openalex/status`, and `/discovery/um-profile` to show readiness.
2. `POST /discovery/preview` to execute and review an adaptive, random, or manual discovery strategy.
3. `POST /runs` with the preview ID to start scoped work.
4. `GET /runs/{run_id}` for stage status.
5. `GET /runs/{run_id}/events` for structured logs.
6. `GET /runs/{run_id}/discovery-candidates` for the retained discovery evidence.
7. `GET /insights` to preview results after matching/export.

The Vite dev server proxies `/api/v1` to `http://localhost:8000`, so local GUI development does not require FastAPI CORS changes.
