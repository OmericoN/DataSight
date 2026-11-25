# DataSight Backend

FastAPI, Celery, Alembic, and pipeline services for the DataSight monorepo.

Follow the [repository quick start](../README.md#quick-start) for infrastructure
and credentials. Run backend development checks from this directory:

```powershell
uv sync --locked
uv run pytest -q
uv run basedpyright
```

## Developer map

- `interfaces/api/main.py` serves the API under `/api/v1`. The running service
  lists request and response schemas at [`/docs`](http://localhost:8000/docs).
  Create a preview with `POST /discovery/preview`, review it, and launch it with
  `POST /runs`. Read progress at `GET /runs/{run_id}` and events at
  `GET /runs/{run_id}/events`.
- `application/discovery_preview.py`, `orchestrator.py`, and `pipeline_services.py`
  build and materialize the pool and coordinate each stage.
- `infrastructure/worker/tasks.py` connects Celery to the stage services. Standard
  runs execute one batch per stage. High-throughput runs validate the PDF target
  first, then advance each paper through conversion, rendering, and inference.
- `application/llm_extraction.py` owns run and publication boundaries.
  `application/catalog_extraction.py` implements the default extractor.
- `domain/extraction.py` defines the evidence contract and its validation.
- `infrastructure/llm/client.py` calls the authenticated CPU gateway; the gateway
  forwards to the separate GPU service.
- `infrastructure/persistence/` stores run state in PostgreSQL, checkpoints
  inference, and publishes complete extraction results atomically.
- `interfaces/api/routes/extractions.py` exposes status, results, and resume.

See [the extraction guide](LLM_EXTRACTION.md) for setup, the evidence contract,
model versioning, manual resume, evaluation exports, and the GPU evaluation gate.
