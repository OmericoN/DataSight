# Contributing

DataSight is a small layered system: a Python backend, a Vite React frontend, and local infrastructure managed by Docker Compose. Keep changes close to the layer that owns the behavior.

## Local Setup

Backend:

```powershell
cd backend
uv sync
uv run alembic upgrade head
uv run datasight doctor
```

Full local stack:

```powershell
docker compose up -d postgres redis grobid
docker compose run --rm migrate
docker compose up api worker
```

Frontend:

```powershell
cd frontend
bun install
bun run dev
```

## Development Workflow

- Create a focused branch for each change.
- Keep bug fixes minimal and covered by regression tests when practical.
- Discuss large features, schema changes, or architecture shifts before implementing them.
- Update docs when behavior, commands, endpoints, settings, or operator workflows change.
- Do not commit secrets. Update `.env.example` when adding configuration.

## Checks

GitHub Actions runs the backend tests and type checks, plus the frontend tests and
production build, on every push and pull request. You can also run the `CI`
workflow manually from the repository's Actions tab. Dependencies are installed
from the committed lockfiles; no repository secrets or running services are needed.

Run backend checks before opening a change:

```powershell
cd backend
uv run pytest -q
uv run basedpyright
```

Run frontend checks when touching `frontend/`:

```powershell
cd frontend
bun run test
bun run build
```

Run Compose validation when changing services, commands, or environment:

```powershell
docker compose config
```

## Boundaries

- `datasight.interfaces` owns HTTP routes and CLI command parsing.
- `datasight.application` owns orchestration, stage services, reset logic, and use cases.
- `datasight.domain` owns shared schemas, stage names, and result objects.
- `datasight.infrastructure` owns SQL, queues, health probes, external HTTP clients, generated files, and GROBID adapters.
- `datasight.matching` owns dataset matching and normalization.
- `frontend/src/features` owns feature workflows.
- `frontend/src/components/ui` owns reusable UI primitives.
- `frontend/src/shared/api` owns API client contracts and request helpers.

Prefer importing through the active `datasight` package. Do not add compatibility imports for old package names.


## Repository Map

```text
backend/    FastAPI, Celery, Alembic, pipeline services, and tests
frontend/   Vite React dashboard for running and monitoring the pipeline
docs/       Focused guides for API use, project rationale, and architecture
data/       Curated source and reference inputs
storage/    Generated runtime artifacts, ignored by git
```

Generated files are written under `storage/`. Curated inputs that should survive resets belong under `data/`.

## Data And Generated Files

Commit stable, curated reference inputs under `data/`. Generated runtime files belong under `storage/` or `outputs/` and should stay out of git.

Reset must remain conservative: it may clear generated database rows and configured generated storage paths, but it must not delete source files, migrations, `.env`, Docker volumes, or curated inputs under `data/`.

## Database Changes

- Put schema changes in `backend/migrations/versions/`.
- Keep Alembic migrations deterministic and reversible where reasonable.
- Run `uv run alembic upgrade head` before tests that depend on schema changes.

## Pull Requests

Before submitting:

- Explain the user-facing or operator-facing behavior change.
- List tests and checks run.
- Mention migrations, reset behavior changes, or new environment variables.
- Include screenshots only when frontend behavior changes materially.
