"""Turn a run-scoped evaluation export into report-safe benchmark tables."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from math import ceil
from pathlib import Path

ATTEMPT_COLUMNS = ["run_key", "paper_id", "extraction_id", "work_key", "request_status", "attempt_number",
                   "http_status", "elapsed_seconds", "prompt_tokens", "completion_tokens",
                   "total_tokens", "error_category"]
SERVING_SETTINGS = {"max_num_seqs", "max_model_len", "max_num_batched_tokens",
                    "gpu_memory_utilization", "dtype", "prefix_caching", "chunked_prefill",
                    "tensor_parallel_size"}
CODE_FILES = (
    "compose.yaml",
    "backend/src/datasight/application/extraction_engine.py",
    "backend/src/datasight/application/bounded_extraction.py",
    "backend/src/datasight/application/high_throughput.py",
    "backend/src/datasight/application/llm_extraction.py",
    "backend/src/datasight/application/pipeline_services.py",
    "backend/src/datasight/domain/extraction.py",
    "backend/src/datasight/infrastructure/llm/client.py",
    "backend/src/datasight/infrastructure/persistence/extractions.py",
    "backend/src/datasight/infrastructure/persistence/items.py",
    "backend/src/datasight/infrastructure/worker/celery_app.py",
    "backend/src/datasight/infrastructure/worker/tasks.py",
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def number(value: str | None) -> float:
    return float(value) if value else 0.0


def summarize(export_dir: Path, output_dir: Path, metadata: dict[str, object]) -> dict[str, object]:
    required = {"run_id", "mode", "gpu", "container_image_digest", "vllm_version",
                "model", "model_revision", "code_revision", "dirty", "serving_settings"}
    if missing := required - metadata.keys():
        raise ValueError(f"Missing metadata: {', '.join(sorted(missing))}")
    if not isinstance(metadata["serving_settings"], dict) or metadata["serving_settings"].keys() - SERVING_SETTINGS:
        raise ValueError("serving_settings contains unsupported fields")
    if metadata["mode"] not in {"diagnostic", "benchmark"}:
        raise ValueError("mode must be diagnostic or benchmark")
    started, finished = metadata.get("started_utc"), metadata.get("finished_utc")
    if metadata["mode"] == "benchmark" and (not started or not finished):
        raise ValueError("Benchmark wall time requires start and finish timestamps")
    wall_seconds = (datetime.fromisoformat(str(finished).replace("Z", "+00:00")) -
                    datetime.fromisoformat(str(started).replace("Z", "+00:00"))).total_seconds() if started and finished else None
    if wall_seconds is not None and wall_seconds <= 0:
        raise ValueError("Finish time must follow start time")
    if output_dir.exists():
        raise FileExistsError(f"Result directory already exists: {output_dir}")

    papers = read_csv(export_dir / "papers.csv")
    extractions = read_csv(export_dir / "extractions.csv")
    attempts = read_csv(export_dir / "request_attempts.csv")
    run_id = str(metadata["run_id"])
    run_key = str(metadata.get('run_key') or '')
    if metadata['mode'] == 'benchmark' and not run_key:
        raise ValueError('Benchmark requires immutable run_key')
    if run_key and run_key not in output_dir.name:
        raise ValueError('Result directory must contain the immutable run_key')
    if not papers or any(row["run_id"] != run_id for row in papers):
        raise ValueError("Paper export does not match the selected run")
    paper_ids = [row["paper_id"] for row in papers]
    if len(paper_ids) != len(set(paper_ids)):
        raise ValueError("Paper export contains duplicate paper IDs")
    extracted = {row["paper_id"]: row for row in extractions}
    if len(extracted) != len(extractions) or not extracted.keys() <= set(paper_ids):
        raise ValueError("Extraction export does not match selected papers")
    by_paper: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in attempts:
        if row["paper_id"] not in paper_ids or row["extraction_id"] != extracted.get(row["paper_id"], {}).get("extraction_id"):
            raise ValueError("Attempt export does not match the latest run extraction")
        by_paper[row["paper_id"]].append(row)

    paper_rows: list[dict[str, object]] = []
    for paper_id in paper_ids:
        extraction = extracted.get(paper_id, {})
        request_rows = by_paper[paper_id]
        status = extraction.get("status") or "not_started"
        metrics = json.loads(extraction.get("metrics") or "{}")
        progress = metrics.get('progress') or {}
        phases = progress.get('calls_by_phase') or {}
        if metrics and status in {"successful", "failed", "interrupted"} and int(metrics.get("attempts", 0)) != len(request_rows):
            raise ValueError(f"Attempt count mismatch for {paper_id}")
        errors = sorted({row["error_category"] for row in request_rows if row["error_category"]})
        if status == "failed" and not request_rows:
            errors.append("source_preparation_or_upstream")
        durations = sorted(number(row["elapsed_seconds"]) for row in request_rows)
        paper_rows.append({
            "run_id": run_id, 'run_key': run_key, "paper_id": paper_id,
            "source_sha256": extraction.get("source_sha256", ""), "status": status,
            "dataset_count": int(number(extraction.get("dataset_count"))),
            'engine_seconds': progress.get('elapsed_seconds', ''),
            'locator_calls': phases.get('locator', ''),
            'shared_feature_calls': phases.get('shared_features', ''),
            'occurrence_calls': phases.get('occurrences', ''),
            'retry_attempts': progress.get('retry_attempts', ''),
            'repair_attempts': progress.get('repair_attempts', ''),
            'call_limit': progress.get('call_limit', ''),
            'time_limit_seconds': progress.get('time_limit_seconds', ''),
            "attempts": len(request_rows),
            "prompt_tokens": sum(int(number(row["prompt_tokens"])) for row in request_rows),
            "completion_tokens": sum(int(number(row["completion_tokens"])) for row in request_rows),
            "total_tokens": sum(int(number(row["total_tokens"])) for row in request_rows),
            "inference_seconds": round(sum(durations), 3),
            "p95_request_seconds": round(durations[ceil(.95 * len(durations)) - 1], 3) if durations else "",
            "missing_usage_attempts": sum(not row["total_tokens"] for row in request_rows),
            "error_categories": ";".join(errors),
        })
    totals = {key: sum(int(row[key]) for row in paper_rows) for key in
              ("attempts", "prompt_tokens", "completion_tokens", "total_tokens", "missing_usage_attempts")}
    run_row = {
        "run_id": run_id, 'run_key': run_key, "mode": metadata["mode"], "selected_papers": len(paper_rows),
        "successful_papers": sum(row["status"] == "successful" for row in paper_rows),
        "zero_result_papers": sum(row["status"] == "successful" and row["dataset_count"] == 0 for row in paper_rows),
        "failed_papers": sum(row["status"] == "failed" for row in paper_rows),
        "interrupted_papers": sum(row["status"] == "interrupted" for row in paper_rows),
        "other_papers": sum(row["status"] not in {"successful", "failed", "interrupted"} for row in paper_rows),
        **totals, "inference_seconds": round(sum(float(row["inference_seconds"]) for row in paper_rows), 3),
        "wall_seconds": round(wall_seconds, 3) if wall_seconds is not None else "",
        "successful_papers_per_hour": round(3600 * sum(row["status"] == "successful" for row in paper_rows) / wall_seconds, 3) if wall_seconds else "",
    }
    output_dir.mkdir(parents=True)
    write_csv(output_dir / "paper_metrics.csv", paper_rows, list(paper_rows[0]))
    write_csv(output_dir / "run_metrics.csv", [run_row], list(run_row))
    write_csv(output_dir / "request_attempts.csv", [{'run_key': run_key, **row} for row in attempts], ATTEMPT_COLUMNS)
    manifest = {key: metadata[key] for key in required}
    manifest['run_key'] = run_key
    root = Path(__file__).resolve().parents[3]
    manifest.update({"started_utc": started, "finished_utc": finished,
                     "source_hashes": {row["paper_id"]: row["source_sha256"] for row in paper_rows},
                     "code_files_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                           for name in CODE_FILES},
                     "raw_exports": {name: {"path": str((export_dir / name).resolve()),
                                            "sha256": hashlib.sha256((export_dir / name).read_bytes()).hexdigest()}
                                     for name in ("papers.csv", "extractions.csv", "request_attempts.csv")},
                     "files_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in sorted(output_dir.iterdir())}})
    (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return run_row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.export_dir, args.output_dir,
                               json.loads(args.metadata.read_text(encoding="utf-8"))), indent=2))
