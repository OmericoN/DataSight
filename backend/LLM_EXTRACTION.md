# Dataset evidence extraction

DataSight owns extraction; the DSRI gateway and vLLM backend remain generic.
`extract_features` scans every selected publication, including papers with no
rules-detector candidates. Automatic workflows still end at extraction.

## Run it

Apply the migration before starting API and worker processes:

```powershell
cd backend
uv run alembic upgrade head
```

Configure the backend environment (never frontend variables or committed secrets):

```text
LLM_BASE_URL=https://datasight-llm-server-ub-ub-datasight.apps.dsri.unimaas.nl
LLM_API_KEY=<existing gateway secret>
LLM_MODEL=Qwen/Qwen3.8-27B
LLM_MODEL_REVISION=1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0
LLM_TIMEOUT_SEC=195
LLM_EXTRACTION_STRATEGY=bounded
LLM_INFERENCE_CONCURRENCY=4
```

`LLM_BASE_URL` is the gateway origin, without `/v1`. Keep revision configuration
aligned with the serving deployment: the gateway's model listing identifies the
served name, not independently the weight revision. Credentials are never put
in run configuration, checkpoints, browser requests, or diagnostic responses.
The bundled tokenizer comes from the pinned [Qwen3.8-27B revision](https://huggingface.co/Qwen/Qwen3.8-27B/tree/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0)
and ships with its [Apache 2.0 license](src/datasight/assets/QWEN-LICENSE.txt).

Start the Compose `worker` and `llm-worker` services. The LLM worker uses one
prefork process by default; `LLM_WORKER_CONCURRENCY` raises the number of
publication tasks it can run at once. Ordinary pipeline work uses the existing
worker. Do not run a local resume concurrently with queued extraction of the
same run.

New Markdown renders default to `pruned`. LLM extraction prepares a separate, content-addressed full-body
snapshot from retained TEI, leaving pruned input untouched. A current, validated
full-body Markdown artifact can be used if TEI is unavailable. Historical
pruned-only papers without TEI produce an explicit preparation failure.

For an existing, inactive run:

```powershell
uv run datasight extract --run-id 123
uv run datasight extractions --run-id 123
uv run datasight resume-extractions --run-id 123
# Synchronous alternative:
uv run datasight resume-extractions --run-id 123 --local
uv run datasight evaluation-export --run-id 123 --output-dir storage/evaluation/run-123
```

Resume uses the frozen source snapshot and validated request checkpoints.
Changed extraction settings require a new run. A successful zero result is a
completed checkpoint, not permission to repeat inference. An in-flight request
lost before its checkpoint commits may need to be generated again.

The default `bounded` strategy scans paragraph-aware blocks targeting 6,000 model
tokens with 300 characters of overlap. It uses exact indexed source anchors and
extracts shared fields once per provisional dataset group. It reuses a single
answer when dataset and occurrence evidence are identical, and budgets batches
of up to eight other occurrences. Each stage dispatches up to
`LLM_INFERENCE_CONCURRENCY` requests per publication so vLLM can batch them.
Keep the publication worker count in mind when changing this limit.
Optional names that cannot be found verbatim in the source are retained as
unnamed references for review. Public extraction fields remain unchanged.

Both strategies now have per-invocation safety budgets: five minutes/50 HTTP
attempts, or 15 minutes/100 attempts for sources over 60,000 characters or more
than 30 located references. Retries and repairs count. A budget interruption
publishes no partial result and exposes `extraction_budget_exceeded`; explicit
resume starts a new invocation budget and reuses successful checkpoints.
Legacy runs keep their original configuration. Start a new run to use the
optimized prompts and batching; completed results remain readable. Extraction
metrics record HTTP attempts and rejected completions, with wall time and
partially rejected responses in invocation progress.

Before starting GPU work, freeze a small preview cohort and validate downloads:

```powershell
$previewId = Read-Host 'Frozen discovery preview ID'
docker compose run --rm --no-deps llm-worker uv run python benchmarks/vllm/download_preflight.py --preview-id $previewId --processing-limit 2
```

The preflight creates a download-only run and never dispatches inference. For
an existing selected cohort, use `--run-id` instead. Public OpenAlex PDF links
are tried in order, then explicit PDF links on retained landing pages. Invalid
bibliography/HTML responses are not retried unchanged; access blocks are
reported without attempting to bypass publisher security.

GPU unavailability interrupts inference without polling or a rules fallback.
Already scheduled ingestion may finish; manual resume is rejected while upstream
item processing is still active. The dashboard has a manual **Check model**
action and **Resume extraction** control. Worker callbacks cannot turn an
interrupted run into a successful one.

## Contract

The typed source of truth is `datasight.domain.extraction`. Schema version:
`dataset-evidence-v2`; default prompt version: `indexed-sparse-v16`.
New results collect dataset identity and use evidence. FAIR attributes are deferred
until after catalogue matching. Earlier completed `v1` results remain readable;
an incomplete `v1` run needs a new run to use the changed contract.

`PublicationExtraction` contains source hash, coverage, datasets, evidence, and
unresolved passages. Every dataset has a publication-local ID and separate
reference occurrences. `Observed[T]` represents each factual field:

```json
{
  "state": "reported",
  "values": [{"value": "Alpha cohort", "evidence_ids": ["e100_140"]}]
}
```

States are `reported`, `not_reported`, `unclear`, and `conflicting`.
`not_reported` has no values. Distinct valid versions or roles are not automatically
a contradiction. Quoted evidence is anchored against the exact immutable
Markdown snapshot, with application-computed character offsets and section/
placement metadata. Offsets are Unicode character indices, not PDF coordinates.

Identity preserves reported names/descriptions, aliases, creators, year,
identifiers and their target, repository/accessions, URLs and their target,
versions, subsets/time slices, and access dates. Use preserves multiple roles,
origin, purpose, claim subject, and description. Occurrences preserve local use,
mention/reference forms, linked or unresolved bibliography pointers, target
type, attribution, and citation components. A bibliography-only occurrence
cannot establish creation or reuse. Article identifiers are distinguished from
dataset identifiers.

Grouping uses explicit dataset identifiers, specific names and supported aliases.
Conflicting identifiers or ambiguous identity links leave separate provisional
entries with review flags. Unnamed entries retain source descriptions rather
than invented titles. Supporting passages that could not be attached remain
available for review.

Literal identity values and evidence references are validated in application
code; the LLM still makes semantic judgments. Grounded quotations alone do not
prove that a role or dataset association is correct. Human evaluation remains
necessary.

## Matching boundary

Extraction retains identity, use, occurrence, and citation evidence. It does not
assess FAIR attributes or verify an external repository. Catalogue matching is a
separate stage; FAIR assessment can follow a supported match.

## Requests, persistence and inspection

The default locator scans overlapping source blocks targeting 6,000 tokens from the
pinned Qwen tokenizer, with paragraph-boundary preference and 300-character overlap.
The opt-in bounded locator retains its indexed 6,000-character segmentation.
Both use exact model-token counts for the full prompt, reserving 2,048 output tokens
and 1,024 template tokens within the 16,384-token context and 40,000-character
gateway limit. Oversized blocks are subdivided. The tokenizer is bundled and checked
against the configured model revision; an unavailable or mismatched tokenizer fails
extraction explicitly. Changed block and prompt budgets require a new run; completed
older results remain readable.
Deploy the gateway's 40,000-character cap before starting a run with this worker;
an older gateway can reject a request that passes the worker's local budget.

Feature requests use relevant passages and bibliography context.
Invalid locator or feature completions get one stricter grounding request before
bounded subdivision. At the minimum block size, a line-indexed locator can
select exact source lines without copying their text; invalid indices or names
still fail explicitly. Oversized/truncated work is also subdivided. Responses require
the configured served model, a normal stop, strict schema and source validation.

Transient failures have at most three attempts per work unit per invocation.
503 interrupts immediately. Long Retry-After waits require later manual resume.
Publication results become visible atomically only after every required request
validates. Requests/attempts remain in PostgreSQL for inspection on failure.
No numeric LLM confidence, FAIR score or formality score is generated.

API endpoints:

- `GET /api/v1/llm/status`
- `GET /api/v1/runs/{run_id}/extractions?offset=0&limit=50`
- `GET /api/v1/runs/{run_id}/extractions/{extraction_id}`
- `POST /api/v1/runs/{run_id}/extractions/resume`

Resume conflicts return 409; missing runs/results return 404. Legacy insight
columns remain available; new fields include extraction status, provenance and
structured features. Legacy results are labeled `legacy` and are never assigned
invented evidence. Run queries use that run's extraction, rather than a global
publication join. Existing reset handles the new tables and snapshot directory.

Evaluation export adds `extractions.json`, `extractions.csv`, `datasets.csv`,
`occurrences.csv`, `field_observations.csv`, and sanitized `request_attempts.csv`
alongside papers/candidates. The attempt CSV has status, duration, token counts,
HTTP status, and bounded error category without prompts, completions, or credentials.
The JSON retains evidence, review flags, source/config fingerprints, coverage,
request metrics, and explicit zero/error outcomes.

The reproducible GPU procedure and results are in
[`benchmarks/vllm/experiment_report.md`](benchmarks/vllm/experiment_report.md).

## Evaluation gate

Use ten English development papers and at least 30 genuine reference passages,
with labels made from PDFs or verified publisher text, not generated Markdown.
Record AI assistance and exclude these papers from later experimental evaluation.
Freeze renderer/detector/prompt/schema/model versions and source hashes.

Measure occurrence precision/recall against verified passage annotations, pairwise
dataset-grouping errors, field-value/association accuracy and unsupported claims.
Report named, implicit, unnamed, created and reused cases separately. Include
zero-result papers and ingestion/extraction failures in the denominator. Measure
wall time, attempts and tokens per paper using request records. Do not substitute
synthetic tests for this gold evaluation or claim experimental readiness without it.

Run tests and types normally. PostgreSQL integration tests are opt-in using
`DATASIGHT_TEST_POSTGRES_URI`, which must point to an isolated database with
`test` in its name. They migrate and remove unique temporary schemas, never the
application database. Production DSRI deployment and GPU booking are separate.
