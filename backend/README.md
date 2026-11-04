# DataSight Backend

FastAPI, Celery, Alembic, and pipeline services for the DataSight monorepo.

Run local backend commands from this directory:

```powershell
uv sync
uv run alembic upgrade head
uv run datasight doctor
uv run pytest -q
uv run basedpyright
```

## LLM extraction

The `extract_features` stage now uses the authenticated DSRI gateway to process
whole publications. Apply the latest migration and configure the backend before
running it. See [LLM_EXTRACTION.md](LLM_EXTRACTION.md) for the schema, setup,
manual resume, API, exports, FAIR interpretation and evaluation gate.

### Developer map

The flow is: selected publication → full-body source snapshot → locate evidence →
extract typed facts → validate citations and source offsets → publish one complete
result. Interrupted requests retain checkpoints for an explicit resume.

- `application/llm_extraction.py` owns the run and publication boundaries.
- `application/extraction_engine.py` implements the original LLM strategy;
  `application/bounded_extraction.py` implements the opt-in, budgeted strategy.
- `domain/extraction.py` defines the evidence contract and its validation.
- `infrastructure/llm/client.py` calls the authenticated CPU gateway; the gateway
  forwards to the separate GPU service.
- `infrastructure/persistence/extractions.py` stores checkpoints and publishes
  validated results atomically.
- `interfaces/api/routes/extractions.py` exposes status, results, and resume.

`legacy` names the original **LLM** strategy, not the former rules extractor.
See the operational guide above before changing a prompt, schema, or fingerprint.
