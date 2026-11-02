import csv
import json

import pytest

from benchmarks.vllm.summarize import summarize


def write_rows(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_summary_counts_all_papers_and_excludes_raw_content(tmp_path):
    source = tmp_path / "export"
    source.mkdir()
    write_rows(source / "papers.csv", [
        {"run_id": "7", "paper_id": "W1"}, {"run_id": "7", "paper_id": "W2"},
    ])
    write_rows(source / "extractions.csv", [
        {"paper_id": "W1", "extraction_id": "3", "status": "successful", "dataset_count": "0", "source_sha256": "abc", "metrics": json.dumps({"attempts": 1})},
        {"paper_id": "W2", "extraction_id": "4", "status": "failed", "source_sha256": "", "metrics": ""},
    ])
    write_rows(source / "request_attempts.csv", [{
        "paper_id": "W1", "extraction_id": "3", "work_key": "hash", "request_status": "successful",
        "attempt_number": "1", "http_status": "200", "elapsed_seconds": "2.5",
        "prompt_tokens": "100", "completion_tokens": "20", "total_tokens": "120",
        "error_category": "", "raw_response": "private research text and secret",
    }])
    metadata = {"run_id": 7, "mode": "benchmark", "gpu": "H100", "container_image_digest": "sha256:abc",
                "vllm_version": "0.32.0", "model": "Qwen", "model_revision": "revision",
                "code_revision": "commit", "dirty": False, "serving_settings": {"max_num_seqs": 1},
                "started_utc": "2026-11-02T10:00:00Z", "finished_utc": "2026-11-02T10:01:00Z",
                "api_key": "secret"}

    result = summarize(source, tmp_path / "results", metadata)

    assert result["selected_papers"] == 2
    assert result["successful_papers"] == result["failed_papers"] == 1
    assert result["zero_result_papers"] == 1
    assert result["successful_papers_per_hour"] == 60
    assert result["total_tokens"] == 120
    manifest = json.loads((tmp_path / "results" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["raw_exports"]) == {"papers.csv", "extractions.csv", "request_attempts.csv"}
    assert all(len(item["sha256"]) == 64 for item in manifest["raw_exports"].values())
    assert all(len(digest) == 64 for digest in manifest["code_files_sha256"].values())
    output = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "results").iterdir())
    assert "private research text" not in output and "secret" not in output
    with pytest.raises(FileExistsError):
        summarize(source, tmp_path / "results", metadata)
