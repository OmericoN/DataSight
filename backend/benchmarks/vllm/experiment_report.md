# DataSight GPU inference diagnosis and throughput study

**Current scope:** PDF download failures and excessive whole-paper extraction work.
The [narrowed implementation record](#narrowed-fix-record-2026-11-02) below supersedes
the earlier tuning proposals. Jev, model replacement, concurrency sweeps and A100
experiments are outside this change. Earlier observations remain historical evidence.

**Latest outcome:** the PDF fallback, bounded recovery and compact occurrence
format are implemented. The [final narrowed comparison](#final-narrowed-comparison-and-decision)
completed **1/3 papers on the first invocation**. A second completed with one
explicit resume; the review stopped at 100 calls. The whole-cohort completion
target and accuracy gate are **not passed**, so bounded extraction remains opt-in.
Final tested application revision: `7f62265923bba773865d4a26e66b520a0dc2e764`.
GPU deployment returned to **0/0 replicas**; local API/workers are running.

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

## Narrowed fix record: 2026-11-02

### Implemented behavior and recorded source

The application source was committed before the final replays at
`06ec0dca7d6e70ff30bfaa317032d89ca301da6c` (`codex/vllm-diagnostic`).
Earlier prototypes are diagnostic evidence, including those that completed quickly.
Unrelated README, documentation and design-QA changes remain in the working tree;
the extraction application files used by the final replay match the recorded commit.
The manifest therefore honestly retains `dirty=true`; `execution.json` distinguishes
stable committed application code from dirty prototype measurements. A source hash
alone is not presented as a reproducible release.

PDF acquisition now deduplicates the preferred URL, retained alternative PDF URLs,
then known landing pages. A landing page is followed only through an explicit public
PDF link or `citation_pdf_url`; at most five links are followed one level deep.
Responses must have PDF magic, a bounded nonempty body and an EOF marker before
atomic publication. This is an envelope/integrity check, not proof of publication
identity or a complete PDF parser; GROBID conversion remains a separate check.
Bibliography/HTML/truncated/oversized responses are not retried. Only temporary
connection/timeout and configured retryable HTTP failures are retried, at most three
attempts per URL. 401/403 and recognized access challenges become `access_blocked`;
long `Retry-After` becomes `retry_deferred`. Nothing bypasses publisher controls.
Attempts include failed alternative and explicit-link requests; cache reuse counts zero.

The fixed original preflight cohort remains two papers (immutable key
`run-9500c0fb-94e6-43e3-9eed-271fd844e075`): W2890139949 reuses its validated
review PDF with zero new HTTP attempts; W4281658583 (LOTUS) remains inaccessible
after three HTTP attempts: bibliography/invalid PDF, then HTML through a known
landing location and its explicit link. Its final category is `html_body`.
This particular failure is not evidence that authentication/security caused it.
Neither paper was silently replaced. A cloned run without `preview_id` previously
exposed an overly broad download query; the query now scopes to its selected
candidate rows, verified against an unrelated publication in PostgreSQL.

The opt-in `bounded` strategy (`indexed-sparse-v14`) selects numbered sentence/line
units and reconstructs exact quotes/offsets from the immutable full-body source.
It targets 6,000 source characters per batch, with units no longer than 1,000
characters and last-unit overlap; section metadata is retained. The historical
`overlap_characters=300` configuration field belongs to the legacy splitter;
bounded overlap follows source-unit boundaries, not a fixed 300-character slice.
Oversized requests split before inference. Full coverage and exact indices must
validate. Anchored duplicates are removed before shared-feature work.

Shared features run once per provisional exact name/identifier group. Unnamed
references and conflicting identifiers remain separate. Evidence is restricted to
that group's occurrences, explicitly matching support and linked bibliography;
unattached support remains inspectable. Shared internal observations use compact
`f/s/v/e` keys. Occurrence fields use fixed numeric keys, typed values, short
uncertainty states and local evidence indices; these hydrate the same public fields.
Response schemas constrain
the requested field paths and cap observation counts, including one-field repairs.
Local use/citation work is batched for up to eight occurrence IDs; missing, duplicate
or unknown IDs fail validation. Bibliography-only occurrences stay distinguishable.

Only invalid fields receive one repair; validated facts and request checkpoints
are retained. Truncation immediately splits evidence rather than requesting the
same oversized output again; fields split only after evidence cannot split further.
Invalid work that still cannot validate fails explicitly. No partial publication
becomes a successful extraction. Model, BF16 and serving limits were unchanged.

Both the guarded legacy strategy and bounded strategy enforce budgets before
each HTTP attempt, including retries/repairs: 50 attempts/300 engine seconds for
ordinary papers, 100/900 for sources over 60,000 characters or more than 30 locator
reference occurrences. Budgets restart only on an explicit resume; validated cached
work requires no new inference. Budget exhaustion saves checkpoints and returns
`interrupted` / `extraction_budget_exceeded`.
Socket timeouts and retry waits are limited by the remaining engine budget; replay
wall time includes source verification and persistence, and excludes export after
extraction returns. Frozen-source runs exclude PDF download and conversion.
Preparation scripts now print separate download, conversion and rendering seconds
for future acquisitions; the original retained cohort's preparation timings were
not recorded and cannot be reconstructed from these inference measurements.
API/workspace progress
shows phase counts, attempts and time budget without changing existing routes.

### Frozen regression inputs and method

| Paper ID | Source characters | Immutable full-body SHA-256 | Evidence check |
|---|---:|---|---|
| W2890139949 | 143,417 | `0e6cc1b33e6219e8abc19a19eb1989fd059ecd7c5a7c38e21fb9a0cd0770d9b2` | Original long review, arXiv 1809.07294 |
| W3159332717 | 44,557 | `fffde07c535989aa4c5acb6a4762a1ae172f0f8c08d0d283918e373091a7da0b` | Retained PDF; Dataset 1/2/3 labels |
| W4308573601 | 43,834 | `443280854dcea329a5ea013c1761863b6529e7493031c2b6ebc64c90097609f0` | PDF body/tables explicitly use Lung3 and Radiogenomics |

The two smaller sources were prepared once with GROBID into source-only run
`run-bf6ca9ab-957e-4be1-92dd-59ec1b1ce612` (local numeric ID 4).
Every replay creates a new immutable run key and copies the selected cohort and
source snapshots; it does not rerender a different source version mid-comparison.
`regression_sources.csv` records the retained PDF, TEI and source paths and SHA-256
checksums, including the actual PDF versions used for these evidence checks.
Only one inference replay runs at a time. H100 sampling observed one running
request, zero waiting, zero preemptions, 100% instantaneous GPU utilization and
74,017/81,559 MiB used. These are sparse samples, not an average utilization or
an A100 measurement. Low KV-cache use plus serial calls explains underused serving
capacity, but the dominant observed application problems were repeated per-reference
feature work, overfragmented locator batches and invalid/truncated recovery work.

PDF inspection and output-difference inspection were AI-assisted. The known labels
are regression sentinels, not an exhaustively reverified occurrence/field gold set.
The earlier automated label/quote matching did not establish such a set. Do not
interpret finding two known names as 100% recall or a schema pass as field precision.

### Diagnostic experiments and rejection decisions

Every row below refers to the exact run key; the denominator is the number of
selected papers in that run, including inaccessible/unstarted papers. HTTP attempts
are the count of exported attempt rows, including repairs/retries. Wall seconds
come from replay start/finish UTC; a dash means no defensible contiguous wall span.
These are diagnostics, not accepted performance configurations.

| Run key (local ID) | Selected papers | HTTP attempts | Wall seconds | Result / reason not accepted |
|---|---:|---:|---:|---|
| `run-9500c0fb-94e6-43e3-9eed-271fd844e075` (1) | 2 | 268 | — | Original legacy review stopped without a result; LOTUS download unavailable |
| `run-8052635a-10d1-44f9-a290-7782cbaf3ee4` (2) | 2 | 21 | 33.851 | Locator name failed grounding; LOTUS source preparation failed |
| `run-0bfa5ea9-612e-47d7-ac25-de8e2ddb0331` (3) | 2 | 100 | 393.529 | Attempt budget contained work; no completed review; other paper unstarted in this early prototype |
| `run-bde2d5f2-77de-4a8b-99b3-e39119f4c5b9` (5) | 1 | 79 | — | Superseded prototype stopped deliberately; checkpoints retained |
| `run-d7210d60-0cc0-4024-a76f-c25488a09fcf` (6) | 1 | 24 | 77.377 | Structural completion rejected: word-index naming errors and empty shared fields |
| `run-201b5b6e-3913-4d23-9d52-95e031a9b646` (7) | 1 | 50 | 305.571 | Same rejected word-index strategy; not an accuracy pass |
| `run-7b239918-8310-4833-a7f6-54a5b901470a` (8) | 1 | 13 | 35.762 | Structural completion rejected: missed known Lung3/Radiogenomics body references |
| `run-e7bda4f5-12f4-41e2-a231-facaf8e083a3` (9) | 1 | 56 | 901.031 | Time budget interrupted repeated feature recovery; no partial output published |
| `run-e7cd00fe-174c-4be9-8ab7-1690a1196adc` (10) | 1 | 3 | 3.127 | DSRI DNS/network outage, no inference tokens; session recovered without a new login |
| `run-457db5b1-91a7-493d-9ef2-25bb0f8b0246` (11) | 1 | 32 | — | Superseded after repeated output-limit failures; operator stop |
| `run-f1b4f8e6-05ee-494a-8626-0f67456f1197` (12) | 1 | 29 | 231.951 | Explicit failure: model invented field paths; valid checkpoints retained |
| `run-61232d94-cf60-4c62-86c1-b231be68ec9f` (13) | 1 | 29 | 223.883 | Explicit failure: a narrow repair regenerated other fields; narrowed response schemas added |
| `run-95f2e621-c5f7-446f-971d-ffe665f5178f` (14) | 1 | 42 | — | Operator stop after occurrence responses included unrequested identity fields; occurrence schemas narrowed too |
| `run-218b96f5-feb2-4752-84ff-20b99c5d4302` (15) | 1 | 54 | 898.130 | Committed v13 candidate interrupted by time budget, only 16 occurrences completed; not adopted |
| `run-480dc0bf-c843-4779-b820-11dfd2f71ff4` (16) | 1 | 14 | — | Operator stop after v13 failed its completion gate; saved-batch protocol testing replaced further comparisons |

Run 15 used recorded commit `34bc4d6530c2da955cd30b30c9b755fe79ce903d`, with
stable execution hashes. Its output-format cost still failed the gate: 27,655
recorded completion tokens, 54 attempts, no successful publication. This is a
failed benchmark, not an accepted throughput improvement. Run 16 used that same
application revision and was deliberately stopped; no contiguous wall comparison
is reported for it.

The saved first occurrence request from run 15 (request 789) was then tested alone,
with the same eight occurrences and evidence. The denominator is one request;
all three probes use the fixed Qwen/H100 configuration. Original dense typed fields
still truncated at 2,048 output tokens (8 occurrences: 64.894 s; 4: 64.889 s).
Fixed short field keys, local numeric evidence indices and minified output validated
all **8/8** supplied IDs plus existing field/evidence checks in one call: **42.313 s,
2,830 prompt tokens, 1,332 completion tokens**. The compact public projection was
not weakened. These are request-level diagnostic results, not whole-paper accuracy.
Raw probe records stay ignored under `storage/evaluation/dense-occurrence-probe*/`.
Their `summary.json` files record raw checksums; the validated probe raw SHA-256 is
`1528026b511d13234606a47e88cf75a6e22e3255a44ee354174d5a2b81ef1c1f`.
This led to the v14 occurrence format in the recorded final application revision.
Atomic publication also now copies the actual prompt version from the frozen run
configuration, instead of leaving the result model's legacy default.

The final recorded replays and adoption decision are below.

### Replicating this narrowed study

Use the booking/login/GPU readiness and smoke procedure above. Treat the earlier
concurrency/server-tuning examples as historical proposals, not steps in this fix.
Do not change vLLM arguments. Never put an `oc` login token in Git, metadata, a
transcript or exported results. Existing `oc whoami` should be checked before asking
for a new token. The posted replacement token was not needed or used by this study.

From the repository root, with the existing database and frozen preview available:

```powershell
$revision = '7f62265923bba773865d4a26e66b520a0dc2e764'
git show --no-patch --format=fuller $revision
git diff $revision -- backend/src backend/migrations compose.yaml frontend/src
docker compose up -d postgres redis grobid api worker
docker compose run --rm migrate
# Download only: substitute the new preview ID; keep its selected cohort fixed.
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/download_preflight.py --preview-id <preview-id> --processing-limit 2
# Or validate a cohort already materialized in the database (ID 1 here).
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/download_preflight.py --run-id 1
# Reserve/start GPU time only after usable PDFs are known; verify the real gateway.
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/smoke.py
# One replay at a time, same immutable source, new run key for each strategy.
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/replay.py --source-run-id 4 --paper-id W4308573601 --code-revision $revision
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/replay.py --source-run-id 4 --paper-id W3159332717 --code-revision $revision
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/replay.py --source-run-id 1 --paper-id W2890139949 --code-revision $revision
# Add --legacy to a new replay to measure the guarded original strategy.
```

Database IDs above are local examples, not portable identities. After a reset,
recover the exported run keys/cohort and seed the source-only cohort with the
retained PDFs; `prepare_replay.py --pdf <absolute-pdf-path>` can be repeated for
each PDF and uses the existing converter. Its resulting source hashes must match
the frozen source table before a comparison is treated as the same experiment.
Keep the original review in its own source cohort. Existing historical intermediate
prompt versions remain historical; do not resume them under newer semantics.

The commands above use the final corrected application revision for a **new**
comparison. The reported GPU matrix used 06ec0dc before the two recovery/retention
corrections; reproducing that exact experiment requires that earlier clean checkout
and its frozen artifacts/database. Do not label a run on the later revision as an
execution of 06ec0dc.

`replay.py` exports automatically under `storage/evaluation/<full-run-key>/` with
UTC timings, execution hashes, configuration and `code_changed_during_run` in
`replay.json`. For a manual run, export using its verified numeric ID:

```powershell
docker compose run --rm --no-deps llm-worker uv run datasight evaluation-export --run-id <id> --output-dir storage/evaluation/<full-run-key>
python backend/benchmarks/vllm/summarize.py --export-dir storage/evaluation/<full-run-key> --output-dir backend/benchmarks/vllm/results/<full-run-key>-comparison --metadata storage/evaluation/<full-run-key>/metadata.json
```

Metadata requires `run_id`, **`run_key`**, `mode`, GPU, image digest, model/revision,
vLLM version, code revision, dirty flag and allowed serving settings; benchmark
mode also requires `started_utc`/`finished_utc`. Copy actual timings/configuration
from `replay.json`, not from a previous run. New result directories must not exist;
their names must contain the full run key. Manifests include source hashes,
whitelisted per-paper extraction configuration, raw export locations/checksums,
and sanitized CSV checksums. Preserve initial execution hashes separately from
the summarizer's current-file hashes when recording old dirty diagnostics.

For an explicit same-configuration resume, use the existing resume endpoint or
`datasight resume-extractions --run-id <id> --local`. Progress per invocation is
distinct from cumulative exported attempts. Never reset a budget invisibly with
automatic redispatch. To stop a local replay, stop its exact one-off container,
then use `mark_interrupted.py` after its exclusive lock is available, as above.

Collect sanitized metric/GPU samples during each replay and record sampling UTC
and run key. Raw samples live under ignored
`storage/evaluation/narrowed-runtime-20261102/`. Export before scale-down; verify
no active replay, no running/waiting model requests and no queued extraction work,
then return `datasight-vllm-gpu` to zero replicas. Keep an active GPU booking for
any later resume; a zero-replica gateway cannot perform extraction.

### Calculations, tests and limits

For each selected paper, `paper_metrics.csv` reports status, zero/nonzero result,
all HTTP attempt rows, tokens, sum of attempt durations, nearest-rank request p95,
engine seconds and phase counts. Phase call counts partition HTTP attempts;
retry/repair counters overlap these phase counts and must not be added again.
For each run, wall seconds = finish UTC minus start UTC, and successful papers/hour
= `3600 * successful_papers / wall_seconds`; failed/interrupted papers remain in
the selected denominator. Missing server usage is counted explicitly, not treated
as verified zero token use. A failed paper can have many retained successful
checkpoints but always has zero published partial datasets.

The benchmark application passed **228 unit/API tests, four opt-in PostgreSQL tests
skipped by the ordinary invocation**. An isolated PostgreSQL test additionally
passed for selected-cohort download scoping, progress persistence, atomic publication,
zero-result export and run isolation. The previously recorded frontend build and
two ExtractionPanel tests passed. Regression tests exercise indexed coverage/offsets,
repeated/unnamed/conflicting identities, evidence association, bad indices/quotations,
truncation, targeted repairs, checkpoint reuse, outage/retry/deadline/attempt limits,
invalid/alternative/access-blocked PDFs, totals and credential exclusion.

Successful stage completion is not an accuracy conclusion. Reference precision/recall,
field precision, association/grouping errors and unsupported-claim rates require
matched PDF annotations and adjudication. Budget interruption proves containment,
not successful extraction. The candidate remains opt-in until the actual regression
completion and verified no-decrease gates pass. Legacy remains the default, with
the same safety budgets. Publisher access failures can remain unresolved.

## Final narrowed comparison and decision

The three sequential first invocations used committed application revision
`06ec0dca7d6e70ff30bfaa317032d89ca301da6c`, Qwen revision and H100/image/settings
recorded above. All four execution-file hashes remained unchanged during each run.
Each row has **one selected paper**. Every paper reached an explicit terminal
outcome; no selected paper is stuck or absent from these exports. Both shorter
papers exceeded 30 locator occurrences and therefore qualified as long under
the agreed rule. The ordinary **300 s / 50-call** gate was not exercised by this
cohort and is not claimed to pass.

| Run key (local ID), paper | Selected | First-invocation outcome | HTTP attempts | Engine seconds | Replay wall seconds | Located work items / published occurrences |
|---|---:|---|---:|---:|---:|---:|
| `run-aea8a73b-6f01-4f0c-a024-027ebf697439` (17), W4308573601 | 1 | Successful | 45 | 474.893 | 474.965 | 44 / 44 |
| `run-8a456a3d-77d7-4093-818e-cc88f1454eb5` (18), W3159332717 | 1 | Time budget interrupted; 96/102 work items checkpointed | 63 | 898.283 | 898.269 | 102 / 0 |
| `run-645fcfb2-3add-493f-9843-f7bffc12ea03` (19), W2890139949 | 1 | Call budget interrupted | 100 | 366.729 | 366.765 | 142 / 0 |

`final_comparison.csv` contains these rows plus phase calls, retries/repairs,
request p95, tokens, missing usage and error categories. For runs 17/18/19,
request p95 was respectively **42.571 / 49.158 / 8.059 seconds** (nearest-rank
over every exported attempt). Recorded completion tokens were **14,752 / 27,324 /
8,573**; missing usage was **0 / 2 / 0 attempts**. Missing usage is not proof of
zero server work. Run 17's 45 calls partition into 10 locator, 29 shared-feature
and 6 occurrence calls; its 10 repairs are already included in those counts.

Across the three first invocations: **208 attempts**, **1 successful / 3 selected
papers**, **2 interrupted**, **0 failed**, **0 omitted/nonterminal**. Cohort wall
time is **1,740.385 s**, from run 17's start UTC to run 19's finish UTC, including
the exports between sequential runs. Successful-paper throughput is
`3600 * 1 / 1740.385 = 2.069 papers/hour`. This describes the tested cohort,
not a general H100 capacity estimate. No additional guarded-legacy performance
replays were run after the candidate failed the completion gate; earlier legacy
and prototype results remain diagnostic comparisons, not a matched accuracy baseline.

The occurrence-format change has a directly matched subtest. The normalized
eight-occurrence input/evidence SHA-256 is identical in both runs:
`b436b1a42916665c7045652ccb3426f2a41cc725c80f9972071748b1da7a86e9`.

| Run key (local ID) | Same occurrence denominator | HTTP attempts including recovery | Sum of request seconds | Completion tokens |
|---|---:|---:|---:|---:|
| `run-218b96f5-feb2-4752-84ff-20b99c5d4302` (15), v13 | 8 | 5 | 233.642 | 7,257 |
| `run-aea8a73b-6f01-4f0c-a024-027ebf697439` (17), v14 | 8 | 1 | 42.571 | 1,327 |

`first_occurrence_batch_comparison.csv` records the exact work keys. Regenerate
these sums by selecting those keys from each run's sanitized `request_attempts.csv`
and summing attempt duration/tokens; subdivided/repair requests are included.
This verifies less recovery for that batch, not an overall accuracy or speedup claim.

### Explicit resume and field-retention check

After preserving run 18's original immutable benchmark export, an explicit resume
at revision `67790a72b8531c2777648abd025f5b3516f3be8a` completed the last six work
items in **29.075 s with one new HTTP attempt**. It reused **61 validated request
checkpoints** and repeated **zero** validated requests. Cumulative attempts became
64. `resume_check.csv` records invocation UTC and counts; the raw resumed export
is separate at ignored
`storage/evaluation/run-8a456a3d-77d7-4093-818e-cc88f1454eb5-resume-check/`.
The original benchmark result remains interrupted. Summing engine intervals
exceeds 15 minutes; this resume is recovery evidence, not a pass of that gate.

The resumed output contains 31 groups and 101 published occurrences: consolidation
coalesced one duplicate anchor among the 102 processed work items. A review of that
path found that duplicate-anchor consolidation could discard the second occurrence's
local features. Revision `7f62265923bba773865d4a26e66b520a0dc2e764` now merges use,
citation and citation-link observations when an anchor is coalesced. A read-only
rebuild from the real cached requests used **zero new HTTP calls**, retained all
790 previous local values and restored **two** additional validated values, with
the same 31 groups/101 anchors. Historical published results were not overwritten.
`cached_consolidation_check.json` records this check.

Recheck retained facts with the current report checkout, the same frozen configuration,
and the actual run key. This command refuses cache misses and cannot issue inference
or overwrite the original publication:

```powershell
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/audit_cached_result.py --run-id 18 --run-key run-8a456a3d-77d7-4093-818e-cc88f1454eb5
```

The final recovery correction also stops retrying a read timeout when the read
timeout was shortened by the publication deadline. The preceding run had spent an
extra attempt near that deadline. Its regression test verifies one attempted call
and immediate resumable budget interruption. Upstream cancellation after client
disconnect was not independently verified; do not assume a timed-out server request
uses zero GPU time. Both post-benchmark corrections are covered by tests; the full
GPU matrix above remains explicitly attributed to revision 06ec0dc.

Final application checks: **230 passed, 4 opt-in PostgreSQL tests skipped** in the
ordinary suite. The isolated PostgreSQL publication/export/progress test passed
separately. The frontend build and two ExtractionPanel tests passed earlier and
the frontend files were unchanged by the final backend corrections. The final
proxied health request reported database, Redis, worker and GROBID ready. Both
Celery workers had no active work and the LLM queue was empty; the old queued
extraction respected its interrupted status and generated no inference. vLLM
showed zero running/waiting requests and zero preemptions before scale-down;
OpenShift then reported `datasight-vllm-gpu` **0/0**, with zero available replicas.

Copyable checks (the PostgreSQL DSN must already identify an isolated database
with `test` in its name; it is deliberately not included in this report):

```powershell
docker compose run --rm --no-deps llm-worker uv run python -m pytest -q
docker compose run --rm --no-deps -e DATASIGHT_TEST_POSTGRES_URI llm-worker uv run python -m pytest -q tests/test_extraction_database.py::test_migration_atomic_publish_zero_result_and_run_isolation
npm --prefix frontend run build
npm --prefix frontend test -- src/features/runs/ExtractionPanel.test.tsx
```

### Acceptance and remaining work

**Do not adopt the candidate as the default.** The completion gate passed for
one of three initial invocations. The review's 126 provisional groups make the
current one-shared-request-per-group approach incapable of completing within
100 calls: 126 shared calls alone exceed the cap, before locator/occurrence work.
Locator input overhead also forced subdivision: the review needed 55 locator
calls despite the nominal 6,000-character target. Those are demonstrated remaining
causes, not GPU OOM or pod-restart failures.

Further work would need to batch shared observations for several independently
keyed groups, reuse one request for shared/local work where evidence permits, and
compact repeated locator metadata while preserving all units. These changes were
**not implemented or accepted in this iteration**. The review remains resumably
incomplete. No broad worker redesign, model replacement, Jev filter, concurrency
sweep or A100 experiment was added.

The two known base datasets in run 17 are present as used by the current paper;
the resumed run contains Dataset 1/2/3 as well. Source-offset checks passed for all
45/106 retained evidence passages in those two completed exports. These checks
do not adjudicate all 19/31 output groups, derived labels, field associations or
unsupported-claim risk. Exhaustive PDF reference/field labels are unavailable for
this matched cohort. Reference recall/precision, factual-field precision, grouping
and association error rates therefore remain **unmeasured**, and no accuracy
no-decrease claim is made. Known names and literal grounding are necessary checks,
not substitutes for that comparison.

The final source, original terminal outcomes, resumed snapshot and sanitized tables
are kept separately. `verification_manifest.json` records the benchmark/final code
revisions, the failed gates, checksums of supplementary tables and locations/checksums
of ignored raw records. `runtime_snapshot.json`, `gpu_samples.csv`,
`protocol_probes.csv`, `regression_sources.csv` and the per-run manifests retain
the hardware, protocol, PDF/TEI/source and execution provenance. None contains
credentials, prompts, raw completions or paper passages. Publisher failures remain
explicit: the original review PDF is usable, while LOTUS still has no validated
public PDF from its retained locations.

Generated CSV/JSON bytes are preserved by the scoped `.gitattributes` rules so
Git cannot change their checksums through platform newline conversion. **82 staged
artifact hashes** and every referenced raw export checksum were verified before
the records commit; credential-pattern checks found no token values in the
versioned benchmark directory. The report itself is versioned by Git alongside
the immutable per-run manifests.
