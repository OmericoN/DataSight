# Technical Architecture

DataSight is an API-first, queue-backed research data pipeline. The backend is a Python `src` layout package, the GUI is a Vite React app, and Docker Compose provides PostgreSQL, Redis, GROBID, the API, and the worker.

## System Shape

```text
GUI / CLI / API
  -> interfaces
  -> application services and orchestrator
  -> domain schemas and stage registry
  -> infrastructure adapters
  -> PostgreSQL, Redis, GROBID, OpenAlex, storage/
```

The FastAPI app starts at `datasight.interfaces.api.main:app`. The CLI starts at `datasight.interfaces.cli.main:main` and is exposed as `uv run datasight`.

## Runtime Components

| Component | Technology | Responsibility |
|---|---|---|
| API | FastAPI | HTTP contracts, OpenAPI docs, run monitoring, reset/import endpoints. |
| Worker | Celery | Long-running stage execution. |
| Queue | Redis | Celery broker and result backend. |
| Database | PostgreSQL + pgvector image | Durable publications, artifacts, mentions, matches, run state, and events. |
| PDF parser | GROBID | PDF to TEI XML conversion. |
| GUI | Vite, React, TypeScript | Operator dashboard for readiness, runs, events, insights, and reset. |
| Migrations | Alembic | Versioned database schema changes. |

## Backend Layout

```text
backend/
  pyproject.toml
  migrations/
  tests/
  src/datasight/
    domain/          shared schemas, stage names, result DTOs
    application/     stage services, full-run orchestration, reset logic
    infrastructure/  persistence, ingestion, worker, health, publication fetching
    interfaces/      FastAPI routes and CLI commands
    matching/        UM dataset matching and normalization
```

Layer rule: interfaces validate input and call application services; application code coordinates behavior; infrastructure owns side effects; domain code stays dependency-light.

## Stage Model

The canonical stage order lives in `datasight.domain.stages`:

```text
discover -> download_pdf -> grobid_convert -> render_document -> detect_mentions -> extract_features -> match_um_dataset -> export_insights
```

`datasight.application.stage_registry` is the shared source for stage descriptions, required arguments, keyword mapping, and Celery task lookup. This keeps API, CLI, and worker behavior aligned.

## Data Flow

1. `discover` materializes an operator-reviewed strategy-v2 preview for every production run. Catalog mode completes direct evidence first, then conditionally runs exact mentions, related works, and focused topic/keyword expansion until its PDF-ready target is met. Random mode uses OpenAlex's native seeded sample operation and preserves the draw order; manual mode runs one expert query. The direct query-first stage remains a development diagnostic only.
2. `download_pdf` downloads and validates open-access PDFs.
3. `grobid_convert` submits PDFs to GROBID and stores TEI XML.
4. `render_document` defaults to complete `full_body` Markdown, including back matter and stable bibliography IDs.
5. `detect_mentions` records high-recall candidates as a comparison baseline; zero candidates do not prevent LLM extraction.
6. `extract_features` scans every selected publication through the authenticated DSRI gateway and extracts evidence-backed dataset entries with separate reference occurrences. Immutable source snapshots, request checkpoints, fingerprints and run-specific linkage support explicit resume and historical inspection.
7. `match_um_dataset` compares mentions against imported UM dataset records and returns all candidate IDs when metadata-only evidence is ambiguous.
8. `export_insights` writes joined insight rows to CSV.

Automatic standard and high-throughput workflows end at `extract_features`.
Matching and export remain registered for explicit use. GPU unavailability leaves
an interrupted, manually resumable run; it never substitutes rules output.
See [the extraction contract and operational guide](../backend/LLM_EXTRACTION.md)
for the schema, configuration, API, exports and evaluation gate. This phase
captures FAIR/citation evidence without assigning compliance or formality scores.

### Adaptive OpenAlex discovery funnel

This Mermaid block is compatible with Obsidian and Excalidraw's Mermaid importer:

```mermaid
flowchart TD
    A["Verified UM dataset catalog"] --> B["Profile catalog identifiers, titles, related works, topics, and keywords"]
    B --> C["Phase 1: Direct evidence"]
    C --> C1["OpenAlex filter: datasets in catalog work IDs; batches of 100"]
    C --> C2["OpenAlex filter: cites catalog work IDs; batches of 100"]
    C1 --> D["Normalize and deduplicate OpenAlex works"]
    C2 --> D
    D --> E{"Enough unique PDF-ready candidates?"}
    E -->|"Yes"| R["Rank and retain candidate pool"]
    E -->|"No"| F["Phase 2: Exact mentions"]
    F --> F1["Exact DOI search; batches of 25"]
    F --> F2["Exact distinctive title and alias search; batches of 5"]
    F1 --> G["Attach a UM dataset ID only when the exact term is visible in returned title or abstract"]
    F2 --> G
    G --> H{"Enough unique PDF-ready candidates?"}
    H -->|"Yes"| R
    H -->|"No"| I["Phase 3A: Catalog-provided related works; batches of 100"]
    I --> J{"Enough unique PDF-ready candidates?"}
    J -->|"Yes"| R
    J -->|"No"| K["Phase 3B: Lazily resolve priority OpenAlex topics"]
    K --> L["Search resolved topics and informative non-generic keywords"]
    L --> M{"Ready target, cost ceiling, or expansion exhausted?"}
    M -->|"Continue"| L
    M -->|"Stop"| R
    R --> R1["Evidence tier: Direct, Exact, or Expanded"]
    R --> R2["Candidate strength: transparent 0 to 100 evidence score; not probability"]
    R1 --> S["Reserve strongest PDF-ready papers up to process-now target"]
    R2 --> S
    S --> T["Fill remaining pool capacity with additional ready papers and no-PDF watchlist leads"]
    T --> U["Operator reviews and excludes candidates"]
    U --> V["Launch standard or high-throughput run with preview ID"]
    V --> W["Revalidate expiry, strategy version, and catalog fingerprint"]
    W --> X["Materialize run-scoped discovery candidates and backfill exclusions"]
    X --> Y["PDF download, full-text extraction, dataset mention detection, and UM matching"]
    Y --> Z["Only downstream evidence may confirm actual dataset use"]
    C -.-> Q{"Next call fits remaining cost budget?"}
    F -.-> Q
    I -.-> Q
    L -.-> Q
    Q -->|"No"| R
```

## Persistence And Artifacts

PostgreSQL is the source of truth for durable state. `PipelineRepository` composes focused persistence modules for publications, discovery previews and candidates, artifacts, mentions, UM datasets, insights, runs, and events.

Generated files are written under:

```text
storage/pdf
storage/xml
storage/markdown
storage/exports
storage/logs
```

Curated source/reference files belong under `data/` and should not be removed by reset.

## API And Worker Flow

`POST /api/v1/runs` creates a pipeline run, stores run configuration, and enqueues Celery work. The GUI polls:

- `GET /api/v1/discovery/um-profile` and `/api/v1/openalex/status` before previewing
- `POST /api/v1/discovery/preview` to create an expiring strategy snapshot
- `GET /api/v1/runs/{run_id}` for run and stage summaries
- `GET /api/v1/runs/{run_id}/events` for chronological messages and errors
- `GET /api/v1/runs/{run_id}/discovery-candidates` for paginated discovery evidence
- `GET /api/v1/insights` for result preview

Celery task names use the `datasight.*` prefix. The worker command is:

```powershell
uv run celery -A datasight.infrastructure.worker.celery_app:celery_app worker --loglevel=INFO --pool=solo -Q celery,download,grobid,processing,matching,export
```

## Configuration

Local configuration comes from `.env` at the repository root. Important defaults:

```env
POSTGRES_DB=datasight_pipeline
OPENALEX_API_URL=https://api.openalex.org
OPENALEX_API_KEY=
OPENALEX_MAILTO=
REDIS_URL=redis://localhost:6379/0
GROBID_BASE_URL=http://localhost:8070
```

Compose uses service names for internal networking and exposes Postgres on `localhost:5433`, Redis on `localhost:6379`, GROBID on `localhost:8070`, and the API on `localhost:8000`.

## Reset Behavior

Reset lives in `datasight.application.reset` and requires:

```json
{
  "confirm": "RESET DATASIGHT",
  "force": true
}
```

It discovers and truncates pipeline-generated application tables in the current database schema, restarts their identity sequences, and deletes configured generated artifact paths under `storage/`. The imported `um_datasets` catalog and Alembic migration marker are preserved. Reset does not delete migrations, source files, `.env`, Docker volumes, or curated inputs in `data/`.

## Failure Modes

- Database readiness failures usually mean migrations have not run or the `.env` database port does not match the runtime.
- Worker readiness failures mean Redis may be up but no Celery worker responded.
- GROBID readiness failures block PDF conversion but not API startup.
- Missing UM dataset records can leave matching/export stages with no meaningful institutional matches.
- Bad PDF URLs, HTML responses served as PDFs, or malformed TEI XML are expected external-data failures and should surface as stage events.
