<p align="center">
  <img
    src="assets/new-banner.png"
    alt="DataSight Logo"
    width="650"
  />
</p>

<div align="center">
  <a href="https://opensource.org/licenses/MIT" target="_blank">
    <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="MIT License">
  </a>
  <a href="https://library.maastrichtuniversity.nl/" target="_blank">
    <img src="https://img.shields.io/badge/Maastricht%20University-Library-002D72?logo=readthedocs&logoColor=white" alt="Maastricht University Library">
  </a>
</div>

<br>

DataSight finds dataset references that are easy to miss in scholarly publications. It discovers papers through OpenAlex and extracts source-backed evidence of dataset use or creation from their PDFs. The project also includes tools for comparing extracted mentions with Maastricht University dataset metadata.

## Processing Pipeline

1. **Discover and review.** Build a candidate pool using the UM catalog, a seeded random sample, or a manual OpenAlex query. Previously processed publications are filtered out before the pool is filled.
2. **Validate and render.** The **Process now** target is a count of validated PDFs. If a download fails, the run tries the next eligible reserve from the reviewed pool and reports any shortfall. GROBID converts validated PDFs to TEI XML, which DataSight renders as Markdown.
3. **Screen and extract.** The model checks whether each paper plausibly used or created a dataset. Likely or uncertain cases proceed to evidence extraction, where results link back to exact passages.

Automatic runs end at evidence extraction. The run page lets you inspect and export results.

## Why It Exists

Dataset reuse is often described in prose instead of cited cleanly in a bibliography. DataSight turns full-text papers into traceable, structured evidence so research support teams can inspect where datasets are used, how they are mentioned, and whether they connect to known institutional datasets.

## Requirements

- Docker with Compose
- Bun and Node.js 22 for the frontend
- uv for host-side Python development or CLI use

## Quick Start

### GUI
Copy [`.env.example`](.env.example) to `.env` in the repository root. Set `OPENALEX_API_KEY` for discovery. Set `LLM_BASE_URL` and `LLM_API_KEY` for live screening and extraction. The [extraction guide](backend/LLM_EXTRACTION.md) describes the model and gateway settings.

From the repository root, start the stack. Compose applies migrations before the API and workers start.

```powershell
docker compose up -d api worker llm-worker
```

The `worker` handles ingestion. The `llm-worker` handles screening and evidence extraction.
Compose publishes the development ports on loopback. The API includes import and reset operations for trusted local use.

Start the GUI in a second terminal:

```powershell
cd frontend
bun install --frozen-lockfile
bun run dev
```

Open the [dashboard](http://localhost:5173), [API docs](http://localhost:8000/docs), or [readiness endpoint](http://localhost:8000/api/v1/health). Import the UM catalog before using catalog-based discovery. Put the input under `data/um_dataset` or select the source in the import workflow.


### CLI

The backend exposes a `datasight` command for operators and development:

```powershell
cd backend
uv run datasight stages
uv run datasight doctor
uv run datasight import-um-datasets --path data/um_dataset
uv run datasight discovery-preview --processing-limit 25 --discovery-limit 250
uv run datasight discovery-preview --mode random --random-seed 42 --processing-limit 25 --discovery-limit 250
uv run datasight run-all --preview-id PREVIEW_UUID --processing-limit 25 --output storage/exports/insights.csv --mode enqueue
```

Full runs require an unexpired discovery preview. Use the preview's ID in `run-all`. A processing limit override may reduce the preview's target. The `--output` argument remains part of the CLI command, while automatic runs publish their results to the database for inspection and export.


## Documentation

- [LLM extraction, evidence schema, setup and evaluation](backend/LLM_EXTRACTION.md)
- [Backend architecture and API entry points](backend/README.md)
- [Contributing](CONTRIBUTING.md)
