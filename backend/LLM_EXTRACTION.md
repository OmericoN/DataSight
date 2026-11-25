# Dataset evidence extraction

DataSight owns extraction; the DSRI gateway and vLLM backend remain generic.
`detect_mentions` now screens selected publications for plausible dataset use or
creation with a short structured decision from the configured model. It uses the
full-body Markdown or reconstructs it from the TEI linked to a pruned render.
It includes qualitative and unnamed data, caption and table evidence, and data
collected by the authors. Bibliography entries are excluded from screening.
A publication is screened out only when every excerpt of its full-body text
receives `no_use`. Likely use, ambiguous evidence, incomplete source text, and
inference failures keep a publication eligible for `extract_features`.

The screening decision is saved with the render hash and detector version.
`New` counts decisions computed in the current screening pass. `Reused` counts
saved decisions for the same render and detector version. Discovery skips
publications with a successful extraction or a terminal negative screening
decision. A paper that failed downstream can still be eligible on a later run.
`papers.csv` includes the decision and the reason in `detection_metrics`.
`rules-v3` and its candidate records remain available for historical runs.

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
LLM_EXTRACTION_STRATEGY=catalog
LLM_INFERENCE_CONCURRENCY=4
```

`LLM_BASE_URL` is the gateway origin, without `/v1`. Keep revision configuration
aligned with the serving deployment: the gateway's model listing identifies the
served name, not independently the weight revision. Credentials are never put
in run configuration, checkpoints, browser requests, or diagnostic responses.
The bundled tokenizer comes from the pinned [Qwen3.8-27B revision](https://huggingface.co/Qwen/Qwen3.8-27B/tree/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0)
and ships with its [Apache 2.0 license](src/datasight/assets/QWEN-LICENSE.txt).

Start the Compose `api`, `worker`, and `llm-worker` services. The LLM worker runs
both screening and extraction and uses one prefork process by default.
`LLM_WORKER_CONCURRENCY` raises the number of publication tasks it can run at once.
Ordinary ingestion uses the existing worker. Do not run a local resume
concurrently with queued extraction of the same run.

Start a **new run** from the GUI to use the default catalog extractor. Open
**Dataset extraction** on the run page and select a paper to inspect its
candidates and exact source evidence. Catalog extraction uses the run's renderer
profile, which defaults to `pruned`. The current renderer keeps scientific back
matter, including dataset methods and availability, alongside compact references.
It stores renders by version and profile, and extraction freezes the same
Markdown hash that passed mention detection.

New runs use `catalog-v6` and `tei-renderer-v8`. The renderer flags dataset
inventory fragments that GROBID places in bibliography entries and preserves
them under **Unparsed appendix table material** for inspection against the PDF.
The extractor excludes that section and ordinary bibliography entries from
inference. A standalone bibliography candidate requires a specific name and a
validated dataset link or identifier. Explicit deposits can still support
datasets identified elsewhere in the source.

The extractor plans contiguous source chunks up to 16,000
characters, then checks each serialized request with the pinned tokenizer.
Catalog extraction sends one chunk at a time and checkpoints each validated response.
Truncated, incomplete, or ungrounded scans can be subdivided up to three levels
per initial chunk. A fact that points outside its own dataset group is omitted
and flagged for review. Grounded facts and complete scan coverage are retained.
The extractor reconciles dataset availability links and explicit deposit
citations across chunks, keeping the source offsets for the resulting aliases,
DOIs, links, creators, and release years. Bibliography-only evidence and clearly
attributed prior work cannot establish use by the current paper.

On explicit resume, saved successful chunks are reused and saved incomplete
parents lead back to their subdivision. Older prompt versions keep their
frozen policies. Fast v1 runs can use compact output as a fallback for a saved
terminal truncation. Successful
chunks are checkpointed, and incomplete papers show a failure reason in the
panel. Candidates with valid evidence and an unsupported name are retained as
unnamed and marked for review. The catalog extractor merges clear repeated names across
chunks, preserves their occurrences, and keeps ambiguous identities and
conflicting versions separate. It resolves uniquely mapped dataset table
labels and narrows shared table evidence when a unique matching cell exists.
The GUI shows candidate counts and total HTTP attempts alongside attempt progress.

`LLM_EXTRACTION_STRATEGY=bounded` selects bounded extraction for new runs.
Existing runs retain their frozen extractor and renderer settings. Bounded,
legacy, and the original `whole_document` strategy use full-body snapshots.

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

The `bounded` strategy scans paragraph-aware blocks targeting 6,000 model
tokens with 300 characters of overlap. It uses exact indexed source anchors and
extracts shared fields once per provisional dataset group. It reuses a single
answer when dataset and occurrence evidence are identical, and budgets batches
of up to eight other occurrences. Each stage dispatches up to
`LLM_INFERENCE_CONCURRENCY` requests per publication so vLLM can batch them.
Keep the publication worker count in mind when changing this limit.
Optional names that cannot be found verbatim in the source are retained as
unnamed references for review. Public extraction fields remain unchanged.

The bounded and legacy strategies have per-invocation safety budgets of five minutes/50 HTTP
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

The typed source of truth is `datasight.domain.extraction`. The schema version is
`dataset-evidence-v2`. Existing runs resume with their saved prompts and
checkpoints. The old `fast` environment setting is an alias for `catalog`.
The bounded strategy collects dataset identity and use evidence. FAIR attributes are deferred
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

Catalog extraction captures names, aliases, dataset DOIs and Handles, landing and
download URLs, accessions, repositories, creators and dataset release years.
Versions, subsets, access dates and short descriptions of the population,
collection site or measurements help a reviewer distinguish similar records.
Bounded extraction also collects use and citation evidence.

The matcher checks direct identifiers and URLs across the catalog. Conflicting
record hits require review. Paper aliases participate in title comparison,
including UM collection titles prefixed with “Data From”. A name or alias match
remains reviewable. Creator and year overlap support the existing similarity
score. Catalog matching remains a separate stage.

### GPU development check, 18 November 2026

Both prompt versions ran on the same pruned Markdown snapshots through the
configured Qwen gateway. Each replay started with empty in-memory checkpoints.
Times include planning, HTTP inference and grounding. They exclude ingestion,
queueing and PostgreSQL writes. Extra values count individual grounded
identity observations beyond the primary name.

| Paper | Version | Candidates | Extra values | HTTP calls | Output tokens | Seconds |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| W4221072223 | fast v2 | 2 | 0 | 1 | 94 | 4.53 |
| W4221072223 | fast v3 | 2 | 3 | 1 | 122 | 4.42 |
| W4392850630 | fast v2 | 7 | 0 | 2 | 244 | 10.45 |
| W4392850630 | fast v3 | 8 | 27 | 2 | 744 | 23.91 |

The v3 alias `NSCLC-Radiomics` on `LUNG1` returned `W6977944434` (“Data From
NSCLC-Radiomics”) for review against the 2,748-record UM export. The extractor
also recovered accession `GSE103584` and the independently cited Stanford
deposit DOI `10.7937/K9/TCIA.2014.X7ONY6B1`. Some descriptions repeated study
details. These development papers were used for prompt tuning. The held-out
evaluation below is still needed to estimate precision, recall and throughput.

## Requests, persistence and inspection

All strategies measure the serialized prompt with the pinned Qwen tokenizer.
The worker reserves 2,048 output tokens and 1,024 template tokens within the
16,384-token context, leaving 13,312 prompt tokens. The gateway also limits
prompts to 40,000 characters. The tokenizer is bundled and checked
against the configured model revision; an unavailable or mismatched tokenizer fails
extraction explicitly. Changed block and prompt budgets require a new run; completed
older results remain readable.
The prompt character count includes message roles. If the gateway reports a
lower character limit, catalog extraction replans oversized chunks for that paper
and saves the reported limit for resume.

Bounded and legacy feature requests use relevant passages and bibliography context.
Invalid locator or feature completions get one stricter grounding request before
bounded subdivision. At the minimum block size, a line-indexed locator can
select exact source lines without copying their text; invalid indices or names
still fail explicitly. Oversized/truncated work is also subdivided. Responses require
the configured served model, a normal stop, strict schema and source validation.

Bounded and legacy transient failures have at most three attempts per work unit
per invocation. Catalog extraction sends one attempt per chunk and uses smaller
chunks to recover from output truncation. A 503 interrupts immediately.
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
