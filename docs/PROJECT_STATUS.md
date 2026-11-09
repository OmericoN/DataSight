# DataSight Project Status

Update, 2026-10-28: whole-publication LLM extraction is implemented with a typed
evidence schema, immutable snapshots, request checkpoints, manual resume,
run-isolated results, API/CLI, exports and dashboard inspection. Backend tests,
frontend tests/build and synthetic schema-constrained DSRI inference passed.
Application migration/configuration and labeled-paper accuracy evaluation remain
operational prerequisites; no application deployment was performed. See
[LLM extraction](../backend/LLM_EXTRACTION.md). The dated assessment below is
historical and describes the earlier rules baseline.

Status date: 2026-08-04  
Target: a demonstrably operating, reviewable pipeline by 2026-08-31.

## Executive assessment

DataSight already has an operational eight-stage pipeline, not just a scaffold. The live stack reports PostgreSQL, Redis, Celery, GROBID, and migrations ready. Two stored standard runs reached all eight stages and produced CSV insights. The authoritative UM catalog is synchronized: 2,748 source records, 2,748 stored records, and no integrity mismatches.

A fresh high-throughput acceptance run (`pipeline_run_id=3`) also completed all eight stages on 2026-08-04 for one publication. It reused or processed one item successfully at every item stage. Its export contained all 50 stored insights, which directly confirms that export is still global rather than run-scoped.

The main remaining risk is result quality and run isolation, not basic service wiring. Historical runs were able to hide partial download failures behind a `successful` status, downstream limits can leave candidates unprocessed, standard runs consume global backlog rather than a run-specific publication set, and the existing 50 insights are all `no_match`. The status reporting defect is fixed in the current worktree. A conservative rules-v2 feature baseline now extracts DOI, URL, year, role, and reference directness, and exact catalog titles/aliases can become reviewable candidates. Existing stored mentions must be reprocessed before those improvements appear in live results.

## Stage review

| Stage | Current state | Evidence | Remaining work |
|---|---|---|---|
| `discover` | Operating | OpenAlex client and profile tests; two live runs discovered 25 publications each. The current worktree adds cost-bounded, expiring discovery previews. | Exercise the preview workflow against the live provider and preserve acceptance evidence. |
| `download_pdf` | Operating with expected external failures | Historical yields were 18/24 and 10/25. Partial failures now produce `completed_with_errors`. | Classify failure reasons, set an acceptable yield target, and add adapter tests for redirects, HTML masquerading as PDF, timeout, and retry. |
| `grobid_convert` | Operating | 28 XML artifacts; live endpoint returns healthy. Docker healthcheck was repaired to avoid unavailable `curl`. | Add integration fixtures and decide whether GROBID 0.8.0 remains the pinned production version. |
| `render_document` | Operating | 28 Markdown artifacts and successful historical stages. | Renderer coverage is low; validate headings, references, tables, figures, malformed TEI, and section counts against fixtures. |
| `detect_mentions` | Operating rules baseline | Historical runs produced 173 and 36 candidates; the demonstrated generic “survey responses” false positive is now excluded. | Build labeled precision/recall evaluation data and tune by section and evidence type. |
| `extract_features` | Operating rules-v2 baseline | Now extracts DOI, URL, a unique year, role, and directness instead of only copying evidence text. | Extract creators, aliases, citations, repository IDs, versions, and access dates; add an explicit reprocessing/versioning mechanism. |
| `match_um_dataset` | Mechanically operating, quality unproven | Catalog integrity is verified; exact DOI/URL, exact title/alias, weighted similarity, and ambiguity handling are implemented. | Reprocess legacy mentions, measure candidate recall and top-k accuracy, tune thresholds, and create a human review workflow. |
| `export_insights` | Operating | 50 stored insight rows and a 39 KB CSV. | Scope exports to a run, emit nested fields as JSON rather than Python-style representations, and include provenance/version columns. |

## Architecture strengths

- Clear interface/application/domain/infrastructure layering.
- One canonical stage order and shared API/CLI/worker stage registry.
- Durable run, stage, item, event, artifact, mention, match, and catalog persistence.
- Standard and database-backed high-throughput strategies.
- Versioned Alembic schema at `20260804_0008` in the current worktree.
- Read-only UM catalog browser and source-versus-database verification.
- Guarded reset behavior that preserves curated inputs.

## Highest-priority gaps

1. **Export isolation.** Standard and high-throughput production runs are preview-backed and restrict downstream processing to included discovery candidates. Confirm that every export query remains run-scoped under concurrent load.
2. **Limit semantics.** Discovery now separates the retained candidate-pool cap from the PDF-ready process target. Downstream mention and feature limits still require load testing when one paper produces many candidates.
3. **Measured quality.** The existing `data/ground_truth` files describe publications but do not label mention spans, normalized dataset identities, or correct UM matches. There is no end-to-end precision/recall or matching benchmark.
4. **Reprocessing/versioning.** Existing candidates and promoted mentions are treated as complete. Updating detector or extractor rules does not automatically refresh stored results.
5. **Operational test depth.** Unit suites are healthy, but coverage is concentrated away from the file/network adapters and full orchestration. The renderer, downloader, converter, standard orchestrator, and failure/retry paths need fixture-backed integration tests.
6. **Oversized run payloads.** Stage metrics persist full per-item results and full OpenAlex seed/query payloads. `GET /runs` can become very large; store summaries in stage metrics and move detailed records behind paginated endpoints.
7. **Deployment boundary.** The API has no authentication and accepts server-local import/export paths. Keep it private for the month-end milestone or add authentication, authorization, and path allow-lists before wider exposure.
8. **Review workflow.** `possible` and `review_required` decisions exist in the model, but there is no accept/reject queue or durable reviewer decision.

## Month-end plan

### August 4–10: make runs trustworthy

- Completed in the current worktree: dead-code/dependency cleanup, partial-failure status fixes, queue-failure lifecycle fixes, and a one-publication high-throughput acceptance run.
- Make high-throughput the documented smoke-test path.
- Scope export to `pipeline_run_id` and stop reusing the publication limit downstream.
- Retain run 3 as the initial smoke-test evidence and add an automated artifact manifest.

### August 11–17: establish quality measurement

- Label 30–50 representative papers with mention spans, dataset names, identifiers, and expected UM IDs/no-match.
- Add a reproducible evaluation command reporting detector precision/recall and matcher top-1/top-k accuracy.
- Separate genuine no-match, insufficient evidence, and extraction failure outcomes.

### August 18–24: improve extraction and review

- Add citation/bibliography linkage and creator/repository feature extraction.
- Tune matcher thresholds from the labeled set.
- Add a small review queue for ambiguous and possible matches.
- Add rule/extractor version fields plus a controlled reprocess command.

### August 25–31: harden and demonstrate

- Run a clean database migration and an isolated end-to-end acceptance run.
- Exercise one download failure, one GROBID failure, worker restart, and idempotent rerun.
- Confirm the GUI shows partial failures and review-required matches accurately.
- Freeze an operator runbook, known-limitations list, and demo dataset.

## Month-end definition of done

- `docker compose up` reaches healthy Postgres, Redis, GROBID, API, and worker services.
- Alembic reports exactly one head and the database is at that head.
- A fresh run reaches all eight stages without manual database edits.
- Partial failures are visible and the final run cannot report false success.
- Results are run-scoped and rerunning does not silently mix or duplicate work.
- The evaluation set produces recorded detector and matcher metrics.
- At least several known positive mentions become correct `matched`, `possible`, or `review_required` outcomes; a pipeline that only emits `no_match` is not accepted.
- Backend tests, type checking, frontend tests, and frontend production build all pass.
