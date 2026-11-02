# DataSight GPU inference diagnosis and throughput study

## Status and provenance

Diagnostic date: **2026-11-02**, Europe/Paris (UTC+01:00). Namespace:
`ub-ub-datasight`. The application repository was at commit
`4b97b01033f9fa2c010d35b406f61fc98bc02300` with uncommitted changes; this
first run is **diagnostic**, not a reproducible release comparison. No credentials,
paper text, generated completions, or unredacted logs belong in this directory.

| Component | Observed value |
|---|---|
| GPU deployment | `datasight-vllm-gpu`, initially 0 replicas |
| Gateway deployment | `datasight-llm-server`, 1 ready CPU replica |
| Gateway before GPU start | authenticated `/v1/models` returned HTTP 503 |
| GPU quota | one permitted, zero in use before start |
| Assigned device | NVIDIA H100 PCIe, 81,559 MiB reported by `nvidia-smi` |
| vLLM | 0.32.0, one GPU, BF16, text-only mode |
| Model | `Qwen/Qwen3.8-27B`, revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` |
| Serving limits | context 16,384; `max_num_seqs=1`; GPU memory utilization 0.90; prefix caching and chunked prefill enabled |
| Image digest | `sha256:696d375800974384d1663c64d20bb1dfef4006a61657c6a0afceb4156eb6c716` |
| GPU pod created | 2026-11-02 11:38:16 UTC; Ready at observation, 0 restarts |

The model pod reached Ready with no restart. A synthetic schema-constrained
locator request through the authenticated gateway returned one anchored passage
in **3.25 s**, HTTP 200, with 497 prompt and 75 completion tokens. This is a
functional smoke check, not a throughput measurement.
The pod event stream contained one startup-probe connection refusal at 11:40:23
UTC while the model was loading. vLLM logged model loading from 11:39:01 and
completion at 11:40:06 (50.13 GiB, 65.45 s), started HTTP serving at 11:40:27,
and reported application startup complete. No OOM, CUDA error, or runtime
restart appeared in the inspected pod status and filtered logs.

## Failure diagnosis

The present 503 was caused by the GPU deployment being scaled to zero. In the
previous stored run (`pipeline_run_id=1`), eight selected papers ended with errors:
three lacked retained TEI or a validated full-body Markdown source, while five
reached inference. Those five recorded 135 requests: 130 completed checkpoints
and five invalid completions. Four invalid locator responses contained a quote
absent from its source block or a dataset name absent from its quote; one identity
response asserted an alias absent from its cited evidence. These categories were
recomputed against the saved source snapshots without exposing the text.

The five retained full-body sources require **230 locator blocks** at the current
2,200-character chunk size, before feature requests. The app previously sent
requests serially, and the server was configured for one active sequence.
During sampled active requests, GPU utilization was 100%; one sample used
73,353/81,559 MiB. The first vLLM metrics snapshot showed one running request,
none waiting, and under 1% of KV cache blocks in use. This supports a
concurrency bottleneck; it does not by itself establish the maximum safe
throughput. One long paper yielded 50 reference and three support passages from
29 successful locator requests, producing hundreds of downstream feature calls.

### Pre-repair diagnostic snapshot

Run ID **1**, captured around 2026-11-02 12:28 UTC before stopping the earlier
worker. Denominator: **8 included papers** (five with full-body sources, three
without). Attempts and tokens are sums of saved request attempts; inference
seconds sum attempt durations and are **not wall time**. The active paper was
incomplete at this snapshot. The worker had loaded code before later repairs;
the manifest's file hashes describe the on-disk state at export, so this is
diagnostic evidence rather than a release comparison.

| Run ID | Selected papers | Successful | Failed | Running | Attempts | Prompt tokens | Completion tokens | Inference seconds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1, pre-repair snapshot | 8 | 0 | 7 | 1 | 983 | 2,422,356 | 84,119 | 3,159.248 |

The running paper alone had 816 recorded attempts, 2,235,564 prompt tokens,
49,589 completion tokens, and 2,039.327 request seconds in this snapshot.
The [run table](results/run-1-pre-repair-diagnostic/run_metrics.csv),
[per-paper table](results/run-1-pre-repair-diagnostic/paper_metrics.csv),
[attempt table](results/run-1-pre-repair-diagnostic/request_attempts.csv), and
[configuration manifest](results/run-1-pre-repair-diagnostic/manifest.json) are
versioned. The immutable raw snapshot is under ignored
`storage/evaluation/run-1-before-resume-20261102T1228Z/`; the manifest records
its CSV locations and checksums. This snapshot has no meaningful throughput
denominator because it spans earlier sessions and ended mid-paper.

### Closing diagnostic snapshot

Run ID **1**, captured 2026-11-02 around 12:44 UTC after the bounded final replay
was stopped and all unfinished extraction records were marked `interrupted` with
their checkpoint totals. Denominator: **8 included papers**; attempts and tokens
sum saved request attempts; inference seconds sum request durations across
sessions. The run has no valid wall-time throughput measure.

| Run ID | Selected papers | Successful | Failed | Interrupted | Attempts | Prompt tokens | Completion tokens | Inference seconds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1, closing diagnostic | 8 | 0 | 5 | 3 | 1,081 | 2,552,530 | 106,255 | 3,890.590 |

The five failures comprise three missing full-body sources and two saved
`Invalid completion: ValueError` outcomes from earlier processing. The three
interrupted papers retain 1,001 request attempts in total and can be resumed;
none has a published partial dataset result. The indexed fallback advanced one
previously failing paper from 61 to 93 saved requests, but the complete paper
was not validated before this session ended. The [closing run CSV](results/run-1-final-diagnostic/run_metrics.csv),
[paper CSV](results/run-1-final-diagnostic/paper_metrics.csv),
[attempt CSV](results/run-1-final-diagnostic/request_attempts.csv), and
[manifest](results/run-1-final-diagnostic/manifest.json) are versioned; raw
exports remain under ignored `storage/evaluation/run-1-final-20261102T1244Z/`.
The final sanitized vLLM snapshot is under ignored
`storage/evaluation/run-1-gpu-diagnostic/vllm-metrics-sanitized-final-20261102T1243Z.prom`
(SHA-256 `504006b69e31af6d5e39aed3579dd0c9f8ef2a4cf9fd0c83293baaf4d94de7bc`).
Before scale-down, vLLM reported zero running and zero waiting requests; the
deployment was returned to **0 replicas**.

The allowlisted metrics snapshots are stored in ignored storage at
`storage/evaluation/run-1-gpu-diagnostic/vllm-metrics-sanitized-20261102T115329Z.prom`
(SHA-256 `26a3c0af471ef16e8b7b7a156631d789c667e3cb3114fa43652b1f1e9e5c714d`)
and `vllm-metrics-sanitized-active-20261102T1217Z.prom` in the same directory
(SHA-256 `2ee17f05d09964edb3c2acd781d36043f473b31ee78c32281b55fbe0e3beb3d2`).
The latter snapshot recorded one running request, none waiting, 2.43% KV-cache
use, 0 preemptions, 1,050,123 prompt tokens and 40,011 generated tokens since
this pod started. Counters include all clients and must be differenced between
snapshots for run throughput.

The repair preserves exact quote and factual-value validation. An invalid
locator or feature response now gets one stricter grounding prompt, then bounded
subdivision. A previously invalid locator block succeeded with the stricter prompt in a
single diagnostic replay (10 anchored passages, 26.89 s, 2,105 tokens). Remaining
invalid work must fail explicitly rather than silently become evidence. In the
first repaired resume, one 255-character block still returned a 176-character
quotation absent even after whitespace normalization; its longest exact span
was only 80 characters. A separate line-indexed probe on that same block
returned four valid source lines (two reference lines) in one request, using
384 prompt and 64 completion tokens. The extraction worker now uses this
line-indexed fallback only when a minimum-sized block cannot validate; the
application reconstructs quotations from the source and rejects bad indices
or unsupported names. Its full-run effect remains to be measured.

## Replication procedure (PowerShell)

Use an active DSRI booking and authenticate locally through the console's
**Copy login command**. Never paste the token into a report or command transcript.
Record the booking window and the exact UTC start and finish of each run.

```powershell
$project = 'ub-ub-datasight'
oc whoami
oc -n $project get deployment datasight-vllm-gpu
oc -n $project get quota gpu-quota
oc -n $project scale deployment/datasight-vllm-gpu --replicas=1
oc -n $project rollout status deployment/datasight-vllm-gpu --timeout=20m
$vllmPod = oc -n $project get pod -l app=datasight-vllm-gpu -o jsonpath='{.items[0].metadata.name}'
oc -n $project exec $vllmPod -c vllm -- nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits
oc -n $project get pod $vllmPod -o jsonpath='{.metadata.creationTimestamp} {.status.containerStatuses[0].ready} {.status.containerStatuses[0].restartCount} {.status.containerStatuses[0].imageID}'
```

From the repository root, start the existing local database. Apply migrations
if the database is behind, then use a **new run ID** for every model or prompt
configuration. The same frozen publication IDs and full-body source hashes must
be used for comparisons. The command below resumes an existing run; record the
actual run ID and do not overlap it with another extraction of that run.

```powershell
docker compose up -d postgres redis
docker compose run --rm migrate
cd backend
uv run python benchmarks/vllm/smoke.py
cd ..
docker compose run --rm --no-deps llm-worker uv run datasight resume-extractions --run-id 1 --local
docker compose run --rm --no-deps llm-worker uv run datasight evaluation-export --run-id 1 --output-dir storage/evaluation/run-1
```

For a deliberate stop of a **local** replay, first identify and stop only its
`llm-worker-run` container. Then mark unfinished paper records interrupted and
resume later. The script takes the run's exclusive extraction lock and refuses
to run while another worker owns it. It retains every request checkpoint.

```powershell
docker ps --filter name=datasight-llm-worker-run --format '{{.Names}}'
$workerContainer = Read-Host 'Exact active replay container name from the listing'
docker stop --timeout 30 $workerContainer
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/mark_interrupted.py --run-id 1
docker compose run --rm --no-deps llm-worker uv run datasight resume-extractions --run-id 1 --local
```

For a short serving-capacity probe, the script selects one deterministic source
block from each retained paper, verifies the source hash, and repeats the same
five schema-constrained requests at each client concurrency. Its CSV contains
only paper/hash/block identifiers, timing, token counts, and status. Record the
deployment settings and a separate output filename for each measurement. This
probe tests serving capacity; it does not replace whole-paper or accuracy runs.

```powershell
cd backend
foreach ($n in 1,2,4,8) {
  uv run python benchmarks/vllm/load_probe.py --export-dir ../storage/evaluation/run-1 `
    --source-dir ../storage/extractions/sources --concurrency $n --repetitions 2 `
    --output "../storage/evaluation/load-probe/seq1-client$n.csv"
}
cd ..
```

For a new high-throughput run, launch the existing preview-backed pipeline with
the same included paper IDs and `full_body` rendering. Set
`LLM_WORKER_CONCURRENCY` to 1, 2, 4, then 8 across separate runs. Record its
actual run ID, code revision, model revision, pod image digest, serving arguments,
GPU, start/finish UTC, and source hashes before comparing configurations.
After all active inference ends, a serving-limit candidate can be deployed for
the next measurement. Scale to zero first because the project has quota for
only one GPU and a rolling update may otherwise request a second pod. The image
entrypoint appends `VLLM_EXTRA_ARGS` after its default `--max-num-seqs 1`;
confirm the effective value in startup logs. Restore the original setting after
the comparison.

```powershell
oc -n $project scale deployment/datasight-vllm-gpu --replicas=0
oc -n $project rollout status deployment/datasight-vllm-gpu --timeout=5m
oc -n $project set env deployment/datasight-vllm-gpu VLLM_EXTRA_ARGS='--max-num-seqs 4'
oc -n $project scale deployment/datasight-vllm-gpu --replicas=1
oc -n $project rollout status deployment/datasight-vllm-gpu --timeout=20m
# Run the same load probe and a new full-paper run with distinct output/run IDs.
oc -n $project scale deployment/datasight-vllm-gpu --replicas=0
oc -n $project rollout status deployment/datasight-vllm-gpu --timeout=5m
oc -n $project set env deployment/datasight-vllm-gpu VLLM_EXTRA_ARGS-
oc -n $project scale deployment/datasight-vllm-gpu --replicas=1
oc -n $project rollout status deployment/datasight-vllm-gpu --timeout=20m
```

Collect vLLM `/metrics` from inside the pod at start, during load, and at finish;
retain raw snapshots under ignored `storage/evaluation/<run-id>/` and record
their SHA-256 hashes. The `request_attempts.csv` export contains only timings,
token counts, HTTP status, and a bounded error category. Keep full extraction
exports in the ignored storage directory.

```powershell
$metricsPath = "storage/evaluation/run-1/vllm-metrics-$(Get-Date -AsUTC -Format yyyyMMddTHHmmssZ).prom"
oc -n $project exec $vllmPod -c vllm -- curl -fsS http://127.0.0.1:8000/metrics |
  Where-Object { $_ -match '^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc|prompt_tokens_total|generation_tokens_total|num_preemptions_total|prefix_cache_hits_total|prefix_cache_queries_total)(\{|\s)' } |
  Set-Content -Path $metricsPath -Encoding utf8
Get-FileHash $metricsPath -Algorithm SHA256
```

Create a metadata JSON file with `run_id`, `mode` (`diagnostic` or `benchmark`),
`gpu`, `container_image_digest`, `vllm_version`, `model`, `model_revision`,
`code_revision`, `dirty`, and `serving_settings`. Benchmarks also require
`started_utc` and `finished_utc` in ISO 8601. Then regenerate the versioned
summary tables in a new output directory:

```powershell
cd backend
uv run python benchmarks/vllm/summarize.py --export-dir ../storage/evaluation/run-1 --output-dir benchmarks/vllm/results/run-1-diagnostic --metadata ../storage/evaluation/run-1/metadata.json
```

`paper_metrics.csv` counts every selected paper, including preparation failures
and zero-result successes (`status=successful`, `dataset_count=0`).
`request_attempts.csv` is one row per saved attempt;
`run_metrics.csv` sums that same cohort. Inference seconds are the sum of request
durations. Successful papers per hour uses the measured run wall time and is
blank for a diagnostic assembled across multiple sessions. The manifest records
publication source hashes, key implementation file hashes, raw export locations
and SHA-256 checksums, and checksums of the versioned CSVs. These file hashes
help audit this diagnostic but are not a substitute for a committed revision.
Do not overwrite a
result directory; create a new one for each configuration.

When no extraction remains active, return the GPU deployment to its initial
idle state:

```powershell
oc -n $project scale deployment/datasight-vllm-gpu --replicas=0
oc -n $project get deployment datasight-vllm-gpu
```

## Comparison and acceptance

Use ten English development papers and at least 30 genuine reference passages
annotated from PDFs or verified publisher text, recording any AI assistance.
Exclude these papers from later experimental evaluation. Report occurrence
precision and recall, dataset grouping errors, field-value and association
accuracy, unsupported claims, and failures in the denominator. Adopt a faster
model or serving configuration only if it completes the selected cohort without
stuck or silently omitted papers and shows no decrease on the labeled accuracy
measures. Report H100 and A100 results separately; this run observed an H100 only.

The five-paper replay was stopped with three papers explicitly interrupted and
two still failed; a completed whole-paper concurrency sweep was not run. No
accuracy or throughput improvement is claimed.

### Accuracy-cohort preflight

`outputs/experiment2_dataset_final_verified.csv` (SHA-256
`c24ebe7c6e74cb8a3d65ed95013eb4b21944ee2b417341c19dec249e171caf45`)
contains ten selected English papers with **46** labeled `used` or `created`
references. These do not overlap the five retained full-body sources in run 1.
The fixed IDs are in `prepare_gold.py`. Its 2026-11-02 acquisition record is
ignored at `storage/evaluation/gold-cohort-20261102/cohort.json` (SHA-256
`b4faab380868dd5689d0e82cb34984646d1008370d830ed811228480cc571eb4`).
Four of ten current OpenAlex PDF links yielded valid PDFs; five returned HTML
and one returned an HTTP client error. Automated normalized exact-quote matching
found 0/20 labeled references in the four downloaded PDFs. This may reflect
version differences or text extraction; it does **not** invalidate the earlier
manual PDF verification. The fetched set is not yet a usable matched-source
accuracy cohort, so recall, field precision, grouping errors and unsupported
claims cannot be honestly reported for this GPU run.

```powershell
cd backend
uv run python benchmarks/vllm/prepare_gold.py `
  --labels ../outputs/experiment2_dataset_final_verified.csv `
  --output-dir ../storage/evaluation/gold-cohort-<new-date>
```

Before a final comparison, recover the originally verified PDF versions or
manually reverify every label against frozen new PDFs, convert those ten papers
to retained full-body sources, and freeze source hashes and annotation IDs.

## Verification and remaining limits

Targeted unit tests: **33 passed** for extraction repair, export totals,
credential exclusion, worker redelivery, source verification, and result
directory immutability. Two isolated PostgreSQL integration tests passed for
request export and advisory-lock/redelivery behavior. `docker compose config`
parsed successfully. Versioned result files were searched for credential and
prompt-content markers without matches.

No full paper completed in the bounded replay, so completion reliability is
still unproven. The H100 serving-limit and client-concurrency sweeps, A100 run,
whole-run throughput comparison, and labeled accuracy comparison were not
completed. The GPU deployment is idle at zero replicas. Resume run 1 only
during a new booking; the three interrupted papers retain checkpoints, while
the two model-stage failures need the indexed fallback replay to finish before
their outcomes can be judged. The three source-preparation failures require
retained full-body sources or a newly selected cohort.

## Improvement decisions

| Step | Action | Evidence or acceptance gate |
|---|---|---|
| Availability | Check booking, quota, replica, readiness, and model listing before launching work; resume interrupted papers from saved checkpoints. | The observed 503 was caused by 0 GPU replicas. Outage and redelivery tests must preserve explicit status. |
| Completion | Keep verbatim quote validation, retry an invalid locator with stricter instructions, then subdivide bounded work. | The previous five invalid completions were grounding errors. A paper is successful only after full source coverage and atomic result publication. |
| Application throughput | Queue one paper per task and measure client concurrency 1, 2, 4, 8 on the same frozen cohort. | Compare whole-run successful papers/hour and request p95 with every selected paper in the denominator. |
| Server throughput | On H100, test `max_num_seqs` 1 then 4 and increase only while memory headroom, queueing, preemptions, and accuracy remain acceptable. Test A100 separately with its actual memory size. | One active sequence and low sampled KV-cache use indicate room to test batching, but no safe optimum is yet measured. |
| Request volume | Profile locator, feature, and repair calls by paper; consider tighter evidence selection or reference grouping only after a labeled comparison. | One paper yielded 50 reference passages and hundreds of feature requests; changing association context can alter accuracy. |
| Adoption | Freeze code, sources, model/image revisions, and labels for the final comparison. | Require no decrease in verified reference/field accuracy and no stuck or silently omitted papers. |

The server tuning gates follow vLLM's [optimization guidance](https://docs.vllm.ai/en/stable/configuration/optimization/)
and [metric definitions](https://docs.vllm.ai/en/stable/design/metrics/):
larger prefill batches can improve throughput but change latency, while
preemptions and KV-cache pressure signal an unsafe capacity setting. The
27B BF16 weights alone require roughly 54 GB decimal before runtime and KV
cache, so a single 40 GB A100 would require a different precision or GPU
topology; an 80 GB A100 still needs its own measured sweep.
