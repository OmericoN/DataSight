# Stage A whole-document extraction

New GUI runs on this branch use fast extraction. The commands below compare the
original single-request Stage A baseline with bounded extraction on the same
frozen publication. The replay CLI without a strategy flag selects bounded extraction.

Run the commands from the DataSight repository root with the database and configured LLM gateway available. For a new source cohort, choose a short paper and freeze its full-body Markdown with the existing GROBID preparation command. Use the returned numeric run ID and paper ID for both replays.

```powershell
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/prepare_replay.py --pdf /app/storage/pdf/short-paper.pdf

$runId = Read-Host "Frozen source run ID"
$paperId = Read-Host "Paper ID"
$revision = git rev-parse HEAD
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/replay.py --source-run-id $runId --paper-id $paperId --code-revision $revision --whole-document
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/replay.py --source-run-id $runId --paper-id $paperId --code-revision $revision
```

Run them serially on the same gateway. Stage A reports to `storage/evaluation/stage-a-<uuid>/replay.json`. The bounded replay reports to `storage/evaluation/<run-key>/`. Match the Stage A paper ID and SHA-256 against `extractions.csv` from the bounded replay before comparing them.

- Compare Stage A's `http_requests` and token counts against the rows for that paper in bounded `request_attempts.csv`. Zero requests mean zero tokens. An attempted request with missing usage has a null token count, and the corresponding cohort total is null if any paper lacks that count.
- Compare the extraction intervals in the two `replay.json` files for overall latency. Stage A also reports elapsed time per paper, and bounded extraction reports engine time under `metrics.progress.elapsed_seconds` in `extractions.csv`.
- Review the grounded quotes and groups against the frozen publication and the bounded `extractions.json`. Stage A counts unique group-and-line pairs and reports proposed identity groups, including uncertain groups. Bounded occurrences can use different passage boundaries. Review missed candidates and incorrect grouping directly against the source.

Stage A sends the whole snapshot in one request when the serialized prompt fits. All three papers in `regression_sources.csv` already exceed the character limit before prompt overhead and can serve as zero-request cases when frozen sources are available. A report marked `complete` means the model stopped normally, reported completion, and supplied valid anchors. The model can still miss a dataset or group references incorrectly. A parseable truncated response can retain grounded evidence and still be marked `incomplete`.
