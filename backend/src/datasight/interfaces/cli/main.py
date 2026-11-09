"""CLI for the queue-backed DataSight pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import redis

from datasight.config import CELERY_BROKER_URL, DEFAULT_UM_DATASETS_PATH
from datasight.application.stage_registry import (
    MissingStageArgument,
    PipelineStage,
    StageRunOptions,
    stage_args,
    stage_kwargs,
    stage_values,
    task_for_stage,
)
from datasight.infrastructure.ingestion.renderer import DEFAULT_RENDER_PROFILE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uv run datasight",
        description="DataSight - queue-backed dataset mention pipeline",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    enqueue = subcommands.add_parser("enqueue", help="Enqueue one Celery stage")
    _add_stage_arguments(enqueue)

    run = subcommands.add_parser("run-local", help="Run one stage synchronously")
    _add_stage_arguments(run)

    import_um = subcommands.add_parser("import-um-datasets", help="Import UM dataset metadata JSON/CSV")
    import_um.add_argument("--path", required=True)

    preview = subcommands.add_parser(
        "discovery-preview", help="Create an adaptive OpenAlex discovery preview"
    )
    preview.add_argument(
        "--mode", choices=["catalog_funnel", "random", "manual"], default="catalog_funnel"
    )
    preview.add_argument("--manual-query")
    preview.add_argument("--random-seed", type=int)
    preview.add_argument("--focus-query", default="")
    preview.add_argument("--from-year", type=int)
    preview.add_argument("--to-year", type=int)
    preview.add_argument("--publication-type", action="append", dest="publication_types")
    preview.add_argument("--language", help="ISO language code; use 'en' for the experiment")
    preview.add_argument("--discovery-limit", type=int, default=500)
    preview.add_argument("--processing-limit", type=int, default=50)
    preview.add_argument("--max-cost-usd", type=float, default=0.25)

    run_all = subcommands.add_parser("run-all", help="Run or enqueue the full pipeline")
    run_all.add_argument("--preview-id", required=True)
    run_all.add_argument("--processing-limit", type=int)
    run_all.add_argument("--exclude-candidate", action="append", dest="excluded_candidate_ids")
    run_all.add_argument("--um-datasets", default=DEFAULT_UM_DATASETS_PATH)
    run_all.add_argument("--output", required=True)
    run_all.add_argument("--mode", choices=["guided", "local", "enqueue"], default="guided")
    run_all.add_argument("--overwrite", action="store_true")
    run_all.add_argument(
        "--render-profile", choices=["full_body", "pruned"], default=DEFAULT_RENDER_PROFILE
    )

    evaluation_export = subcommands.add_parser(
        "evaluation-export", help="Export run-scoped papers and active rules-v3 windows"
    )
    evaluation_export.add_argument("--run-id", required=True, type=int)
    evaluation_export.add_argument("--output-dir", required=True)

    subcommands.add_parser("worker-command", help="Print the Celery worker command")
    subcommands.add_parser("stages", help="Print canonical stage order")
    subcommands.add_parser("doctor", help="Check database schema readiness")

    for name in ("extract", "resume-extractions"):
        extraction = subcommands.add_parser(name, help="Extract or resume selected publications in an existing run")
        extraction.add_argument("--run-id", type=int, required=True)
        extraction.add_argument("--local", action="store_true", help="Run synchronously instead of queueing")

    inspect_extractions = subcommands.add_parser("extractions", help="Inspect publication extraction outcomes")
    inspect_extractions.add_argument("--run-id", type=int, required=True)

    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command in {"extract", "resume-extractions"}:
        from datasight.application.llm_extraction import extraction_configuration, queue_resume, require_compatible_run, resume_extraction
        from datasight.infrastructure.persistence.repository import PipelineRepository

        if args.local:
            with PipelineRepository() as repo:
                if not repo.extraction_lock(args.run_id):
                    raise ValueError("This run already has active extraction")
                require_compatible_run(repo, args.run_id)
                repo.cursor.execute("SELECT config FROM publication_extractions WHERE pipeline_run_id=%s", (args.run_id,))
                if any(row["config"] not in (extraction_configuration(legacy=False), extraction_configuration(legacy=True)) for row in repo.cursor.fetchall()):
                    raise ValueError("Extraction configuration changed; start a new run")
                repo.claim_extraction_resume(args.run_id)
            result = resume_extraction(args.run_id)
        else:
            result = queue_resume(args.run_id)
        print(json.dumps(result, indent=2, default=str))
        return

    if args.command == "extractions":
        from datasight.infrastructure.persistence.repository import PipelineRepository

        with PipelineRepository() as repo:
            items, total = repo.list_extractions(args.run_id, limit=100)
        print(json.dumps({"items": items, "total": total}, indent=2, default=str))
        return

    if args.command == "worker-command":
        print("uv run celery -A datasight.infrastructure.worker.celery_app:celery_app worker --loglevel=INFO --pool=solo")
        return

    if args.command == "stages":
        print(json.dumps(stage_values(), indent=2))
        return

    if args.command == "doctor":
        print(json.dumps(_doctor(), indent=2, default=str))
        return

    if args.command == "import-um-datasets":
        from datasight.application import pipeline_services as services

        print(json.dumps(services.import_um_datasets(args.path), indent=2, default=str))
        return

    if args.command == "discovery-preview":
        print(json.dumps(_discovery_preview(args), indent=2, default=str))
        return

    if args.command == "run-all":
        print(json.dumps(_run_all(args), indent=2, default=str))
        return

    if args.command == "evaluation-export":
        from datasight.application import pipeline_services as services

        print(
            json.dumps(
                services.export_evaluation_bundle(args.run_id, args.output_dir),
                indent=2,
                default=str,
            )
        )
        return

    if args.command == "enqueue":
        result = _enqueue(args)
    elif args.command == "run-local":
        result = _run_local(args)
    else:
        raise ValueError(f"Unsupported command: {args.command}")

    print(json.dumps(result, indent=2, default=str))


def _add_stage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("stage", choices=stage_values())
    parser.add_argument("--query")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--topic-id", action="append", dest="topic_ids")
    parser.add_argument("--keyword-term", action="append", dest="keyword_terms")
    parser.add_argument("--from-year", type=int)
    parser.add_argument("--to-year", type=int)
    parser.add_argument(
        "--use-um-profile",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--all-access", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--delete-pdf", action="store_true")
    parser.add_argument("--render-profile", choices=["full_body", "pruned"], default=DEFAULT_RENDER_PROFILE)
    parser.add_argument("--payload-json", help="Deprecated: only used for export rows")
    parser.add_argument("--output", help="Output path for export_insights")


def _doctor() -> dict[str, Any]:
    from datasight.infrastructure.health.probes import celery_worker_available
    from datasight.infrastructure.persistence.repository import PipelineRepository

    with PipelineRepository() as repo:
        health = repo.healthcheck()
    health["redis_ready"] = _redis_ready()
    health["worker_ready"] = celery_worker_available()
    return health


def _enqueue(args: argparse.Namespace) -> dict[str, Any]:
    from datasight.infrastructure.worker import tasks

    task = task_for_stage(args.stage, tasks)
    options = _stage_options(args)
    try:
        async_result = task.delay(*stage_args(args.stage, options), **stage_kwargs(args.stage, options))
    except MissingStageArgument as exc:
        raise SystemExit(str(exc)) from exc
    return {"task_id": async_result.id, "stage": args.stage, "status": "queued"}


def _run_local(args: argparse.Namespace) -> dict[str, Any]:
    from datasight.application import pipeline_services as services

    stage = PipelineStage(args.stage)
    if stage == PipelineStage.DISCOVER:
        return services.discover_publications(
            query=_require(args.query, "--query"),
            limit=args.limit or 100,
            open_access_only=not args.all_access,
            topic_ids=args.topic_ids,
            keyword_terms=args.keyword_terms,
            from_year=args.from_year,
            to_year=args.to_year,
            use_um_profile=args.use_um_profile,
        )
    if stage == PipelineStage.DOWNLOAD_PDF:
        return services.download_pdf_batch(limit=args.limit, overwrite=args.overwrite)
    if stage == PipelineStage.GROBID_CONVERT:
        return services.grobid_convert_batch(limit=args.limit, overwrite=args.overwrite, delete_pdf=args.delete_pdf)
    if stage == PipelineStage.RENDER_DOCUMENT:
        return services.render_document_batch(
            limit=args.limit, overwrite=args.overwrite, profile=args.render_profile
        )
    if stage == PipelineStage.DETECT_MENTIONS:
        return services.detect_mentions_batch(limit=args.limit)
    if stage == PipelineStage.EXTRACT_FEATURES:
        return services.extract_features_from_candidates(limit=args.limit)
    if stage == PipelineStage.MATCH_UM_DATASET:
        return services.match_um_dataset_batch(limit=args.limit)
    if stage == PipelineStage.EXPORT_INSIGHTS:
        return services.export_insights_csv(_require(args.output, "--output"), _payload_rows(args) or None)
    raise ValueError(f"Unsupported stage: {args.stage}")


def _run_all(args: argparse.Namespace) -> dict[str, Any]:
    from datasight.application import orchestrator

    health = _doctor()
    if not health["ready"]:
        raise SystemExit("Database is not ready. Run migrations first: uv run alembic upgrade head")

    mode = args.mode
    if mode == "guided":
        mode = _prompt_run_mode(worker_ready=health["worker_ready"], redis_ready=health["redis_ready"])

    if mode == "enqueue":
        if not health["redis_ready"]:
            raise SystemExit("Redis is not reachable; cannot enqueue the full run.")
        if not health["worker_ready"]:
            raise SystemExit("No Celery worker responded; start a worker before enqueueing run-all.")
        return orchestrator.enqueue_run_all(
            preview_id=args.preview_id,
            processing_limit=args.processing_limit,
            excluded_candidate_ids=args.excluded_candidate_ids,
            output=args.output,
            um_datasets_path=args.um_datasets,
            overwrite=args.overwrite,
            render_profile=args.render_profile,
        )

    return orchestrator.run_all_local(
        preview_id=args.preview_id,
        processing_limit=args.processing_limit,
        excluded_candidate_ids=args.excluded_candidate_ids,
        output=args.output,
        um_datasets_path=args.um_datasets,
        overwrite=args.overwrite,
        render_profile=args.render_profile,
    )


def _discovery_preview(args: argparse.Namespace) -> dict[str, Any]:
    from datasight.application.discovery_preview import create_discovery_preview

    if args.mode == "manual" and not (args.manual_query or "").strip():
        raise SystemExit("--manual-query is required when --mode manual is selected")
    if args.processing_limit > args.discovery_limit:
        raise SystemExit("--processing-limit cannot exceed --discovery-limit")
    preview = create_discovery_preview(
        {
            "strategy_version": 2,
            "mode": args.mode,
            "focus_query": args.focus_query,
            "manual_query": args.manual_query,
            "random_seed": args.random_seed,
            "from_year": args.from_year,
            "to_year": args.to_year,
            "publication_types": args.publication_types or [],
            "language": args.language,
            "discovery_limit": args.discovery_limit,
            "processing_limit": args.processing_limit,
            "max_cost_usd": args.max_cost_usd,
        }
    )
    return {
        "preview_id": preview["preview_id"],
        "strategy_fingerprint": preview["strategy_fingerprint"],
        "catalog_fingerprint": preview["catalog_fingerprint"],
        "code_version": preview["code_version"],
        "provider_snapshot_at": preview["provider_snapshot_at"],
        "language": preview["language"],
        "random_seed": preview["random_seed"],
        "expires_at": preview["expires_at"],
        "stop_reason": preview["stop_reason"],
        "candidate_count": preview["candidate_count"],
        "ready_count": preview["ready_count"],
        "watchlist_count": preview["watchlist_count"],
        "included_count": preview["included_count"],
        "actual_calls": preview["actual_calls"],
        "actual_cost_usd": preview["actual_cost_usd"],
        "warnings": preview["warnings"],
    }


def _prompt_run_mode(worker_ready: bool, redis_ready: bool) -> str:
    if not sys.stdin.isatty():
        return "enqueue" if worker_ready and redis_ready else "local"
    default = "enqueue" if worker_ready and redis_ready else "local"
    prompt = f"Run mode [{default}]? Type 'enqueue' or 'local': "
    answer = input(prompt).strip().lower()
    return answer if answer in {"enqueue", "local"} else default


def _redis_ready() -> bool:
    try:
        client = redis.Redis.from_url(CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1)
        return bool(client.ping())
    except Exception:
        return False


def _stage_options(args: argparse.Namespace) -> StageRunOptions:
    return StageRunOptions(
        query=args.query,
        limit=args.limit,
        output_path=args.output,
        overwrite=args.overwrite,
        delete_pdf=args.delete_pdf,
        open_access_only=not args.all_access,
        topic_ids=args.topic_ids,
        keyword_terms=args.keyword_terms,
        from_year=args.from_year,
        to_year=args.to_year,
        use_um_profile=args.use_um_profile,
        rows=_payload_rows(args) or None,
        render_profile=args.render_profile,
    )


def _payload_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not args.payload_json:
        return []
    payload = json.loads(args.payload_json)
    if isinstance(payload, dict):
        return payload.get("rows") or [payload]
    return payload


def _require(value: str | None, flag: str) -> str:
    if not value:
        raise SystemExit(f"{flag} is required for this command")
    return value


if __name__ == "__main__":
    main()
