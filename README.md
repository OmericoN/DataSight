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

DataSight finds dataset references that are easy to miss in scholarly publications. It discovers open-access papers, downloads PDFs, converts them into structured text, detects dataset mentions, and matches those mentions against Maastricht University dataset metadata.

## Processing Pipeline

![DataSight Processing Pipeline](assets/datasight-pipeline.png)

## Why It Exists

Dataset reuse is often described in prose instead of cited cleanly in a bibliography. DataSight turns full-text papers into traceable, structured evidence so research support teams can inspect where datasets are used, how they are mentioned, and whether they connect to known institutional datasets.

## Requirements

- Docker
- uv
- Bun

## Quick Start

### GUI
Start the backend stack:

```powershell
docker compose up -d postgres redis grobid
docker compose run --rm migrate
docker compose up api worker
```

Open the API docs:

```text
http://localhost:8000/docs
```

Start the GUI in a second terminal:

```powershell
cd frontend
bun install
bun run dev
```

Open the GUI:

```text
http://localhost:5173
```

Check readiness:

```powershell
curl http://localhost:8000/api/v1/health
```


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

Production full runs require an unexpired discovery preview from the adaptive funnel,
seeded random sampler, or advanced manual query. Direct
query-first discovery remains available only through the single-stage command
for development diagnostics.


## Documentation

- [API Usage](docs/API_USAGE.md)
- [Why DataSight](docs/WHY_DATASIGHT.md)
- [Technical Architecture](docs/TECHNICAL_ARCHITECTURE.md)
- [Contributing](CONTRIBUTING.md)
