"""Plan budgeted source chunks and extract grounded dataset candidates."""

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import StrictBool, StrictInt

from datasight import config
from datasight.application.extraction_engine import consolidate
from datasight.application.catalog_features import CATALOG_PROMPT, ROLE_PROMPT, CatalogChunkResponse, CatalogUseChunkResponse, attach_catalog_facts, has_deposit_marker
from datasight.application.catalog_candidate_resolution import refine_candidates
from datasight.application.catalog_identity import has_catalog_deposit_link, has_explicit_deposit_citation, reconcile_catalog_identity
from datasight.application.whole_document_extraction import (
    CandidateGroup,
    LOCATE_GROUPS,
    WholeDocumentResponse,
    ground_groups,
    publication_result,
    saved_completion,
)
from datasight.domain.extraction import PublicationExtraction, StrictModel
from datasight.infrastructure.ingestion.renderer import UNPARSED_TABLE_HEADING
from datasight.infrastructure.llm.client import (
    MAX_PROMPT_CHARS, GatewayClient, InferenceError, InferenceUnavailable, InvalidCompletion,
    OutputTruncated, RequestTooLarge, payload_for, prompt_character_count,
)

PROMPT_VERSION = "catalog-v6"
CATALOG_V5 = "catalog-v5"
CATALOG_V4 = "catalog-v4"
PREVIOUS_CATALOG_V4 = "fast-catalog-v4"
CATALOG_V3 = "fast-catalog-v3"
CANDIDATE_PROMPT_VERSION = "fast-candidates-v2"
LEGACY_PROMPT_VERSION = "fast-candidates-v1"
CATALOG_PROMPT_VERSIONS = (
    PROMPT_VERSION, CATALOG_V5, CATALOG_V4, PREVIOUS_CATALOG_V4, CATALOG_V3, CANDIDATE_PROMPT_VERSION, LEGACY_PROMPT_VERSION,
)
CATALOG_SPLIT_VERSIONS = {PROMPT_VERSION, CATALOG_V5}
CATALOG_TARGET_CHARS = 16_000
CATALOG_SPLIT_DEPTH = 3
MATCHING_PROMPT = ROLE_PROMPT + """
Keep group evidence concise. Use the strongest lines that identify the dataset or establish use,
and avoid repeated summaries, numeric result rows and background mentions. Give repository
collection links and explicit data citations priority. A phrase describing multiple datasets is
not an alias. Pair each link and citation with its own dataset. Leave a fact out if the line does
not establish the association. A cited study's use never establishes use by this publication.
"""
GROUNDED_MATCHING_PROMPT = MATCHING_PROMPT + """
The supplied lines may have gaps in their numbering. Review only the supplied lines.
If there are no datasets in them, return {"c":true,"g":[]}.
Bibliography lines are supplied only for possible data deposits. Emit a standalone
bibliography group only for a specific named deposit with its own dataset URL or identifier.
Group repeated descriptions with a named dataset only when the source explicitly links them.
"""
SECTION_HEADING = re.compile(r"(?m)^#{1,6}[ \t]+([^\r\n]+)")
UNPARSED_TABLE_SECTION = UNPARSED_TABLE_HEADING.removeprefix("## ").casefold()
LOCATE_CHUNK = LOCATE_GROUPS.replace("entire publication", "entire supplied source segment")
LOCATE_COMPACT = LOCATE_CHUNK.replace("Set complete", "Set c").replace("set complete", "set c") + """
Return c for scan completion and g for groups. Each group is [name or null,
identity uncertainty, line IDs]. Emit minified JSON on one line without indentation.
"""


INCOMPLETE_SCAN = "Invalid completion: model reported an incomplete scan"


class IncompleteScan(InvalidCompletion):
    pass


class SavedSplitRequired(OutputTruncated):
    """A saved partial scan requires smaller chunks."""


class CompactChunkResponse(StrictModel):
    c: StrictBool
    g: list[tuple[str | None, StrictBool, list[StrictInt]]]

    def expand(self) -> WholeDocumentResponse:
        return WholeDocumentResponse(complete=self.c, groups=[
            CandidateGroup(name=name, identity_uncertain=uncertain, line_ids=ids)
            for name, uncertain, ids in self.g
        ])


FORMATS = {
    PROMPT_VERSION: (CatalogUseChunkResponse, GROUNDED_MATCHING_PROMPT, ":catalog-use-v6"),
    CATALOG_V5: (CatalogUseChunkResponse, MATCHING_PROMPT, ":catalog-use-v5"),
    CATALOG_V4: (CatalogUseChunkResponse, ROLE_PROMPT, ":catalog-use"),
    PREVIOUS_CATALOG_V4: (CatalogUseChunkResponse, ROLE_PROMPT, ":catalog-use"),
    CATALOG_V3: (CatalogChunkResponse, CATALOG_PROMPT, ":catalog"),
    CANDIDATE_PROMPT_VERSION: (CompactChunkResponse, LOCATE_COMPACT, ":compact"),
    LEGACY_PROMPT_VERSION: (WholeDocumentResponse, LOCATE_CHUNK, ""),
}


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    lines: list[str]
    payload: dict[str, Any]
    prompt_version: str = LEGACY_PROMPT_VERSION
    scan_line_ids: frozenset[int] | None = None

    @property
    def key(self) -> str:
        prefix = "catalog" if self.prompt_version in {PROMPT_VERSION, CATALOG_V5, CATALOG_V4} else "fast"
        return f"{prefix}:{self.start}:{self.end}" + FORMATS[self.prompt_version][2]


def _scan_lines(text: str, start: int, lines: list[str]) -> list[tuple[int, str]]:
    heading = ""
    if start:
        for match in SECTION_HEADING.finditer(text, 0, start):
            heading = match[1].strip().casefold()
    selected = []
    offset = start
    for i, line in enumerate(lines, 1):
        starts_line = offset == 0 or text[offset - 1] in "\n\r"
        offset += len(line)
        ends_line = line.endswith(("\n", "\r")) or offset == len(text) or text[offset] in "\n\r"
        if starts_line and ends_line and (match := SECTION_HEADING.match(line)):
            heading = match[1].strip().casefold()
            if heading in {"references", UNPARSED_TABLE_SECTION}:
                continue
        if not line.strip() or heading == UNPARSED_TABLE_SECTION:
            continue
        if heading == "references" and not (starts_line and ends_line and has_catalog_deposit_link(line)):
            continue
        selected.append((i, line))
    return selected


def make_chunk(text: str, start: int, end: int, max_prompt_chars: int, *, prompt_version: str = LEGACY_PROMPT_VERSION) -> Chunk:
    lines = text[start:end].splitlines(keepends=True)
    model, instruction, _ = FORMATS[prompt_version]
    selected = _scan_lines(text, start, lines) if prompt_version == PROMPT_VERSION else list(enumerate(lines, 1))
    scan_line_ids = frozenset(i for i, _ in selected) if prompt_version == PROMPT_VERSION else None
    if scan_line_ids == frozenset():
        return Chunk(start, end, lines, {}, prompt_version, scan_line_ids)
    data: dict[str, Any] = {"lines": selected}
    if issubclass(model, CatalogChunkResponse):
        deposits = [
            i for i, line in data["lines"] if line.startswith("- **[")
            and ("DOI:" in line or "URL:" in line)
            and (has_deposit_marker(line) or (prompt_version in CATALOG_SPLIT_VERSIONS and has_explicit_deposit_citation(line)))
        ]
        if deposits:
            data["possible_data_deposit_lines"] = deposits
            instruction += "\nCheck each possible_data_deposit_lines entry. Extract confirmed data citations with DOI/URL, title, creators and release year. Skip software and articles."
    payload = payload_for(
        instruction, data, model,
        max_prompt_chars=max_prompt_chars,
    )
    return Chunk(start, end, lines, payload, prompt_version, scan_line_ids)


def preferred_end(text: str, start: int, end: int) -> int:
    lower = start + (end - start) * 3 // 4
    for separator in ("\n\n", "\r\n\r\n", "\n", " "):
        boundary = text.rfind(separator, lower, end)
        if boundary >= 0:
            return boundary + len(separator)
    if end - start > 1 and text[end - 1:end + 1] == "\r\n":
        return end - 1
    return end


def plan_chunks(text: str, start: int, stop: int, max_prompt_chars: int, *, prompt_version: str = LEGACY_PROMPT_VERSION) -> list[Chunk]:
    target = min(max_prompt_chars, CATALOG_TARGET_CHARS) if prompt_version in CATALOG_SPLIT_VERSIONS else max_prompt_chars
    if start == stop:
        return [make_chunk(text, start, stop, max_prompt_chars, prompt_version=prompt_version)]
    chunks = []
    while start < stop:
        low, high = start + 1, min(stop, start + target)
        try:
            best = make_chunk(text, start, high, max_prompt_chars, prompt_version=prompt_version)
        except RequestTooLarge:
            best = None
            while low <= high:
                end = (low + high + 1) // 2
                try:
                    best = make_chunk(text, start, end, max_prompt_chars, prompt_version=prompt_version)
                except RequestTooLarge:
                    high = end - 1
                else:
                    low = end + 1
        if best is None:
            raise RequestTooLarge("Catalog extraction prompt cannot fit even a single source character")
        if best.end < stop:
            boundary = preferred_end(text, start, best.end)
            if boundary < best.end:
                try:
                    best = make_chunk(text, start, boundary, max_prompt_chars, prompt_version=prompt_version)
                except RequestTooLarge:
                    pass  # Keep the already validated span if token boundaries change.
        chunks.append(best)
        start = best.end
    return chunks


def ground_chunk(text: str, chunk: Chunk, response: WholeDocumentResponse | CompactChunkResponse | CatalogChunkResponse) -> dict[str, Any]:
    if isinstance(response, CatalogChunkResponse):
        blank_lines = {i for i, line in enumerate(chunk.lines, 1) if not line.strip()}
        excluded_lines = (
            set(range(1, len(chunk.lines) + 1)) - chunk.scan_line_ids
            if chunk.scan_line_ids is not None else set()
        )
        ignored_lines = blank_lines | excluded_lines
        response = response.model_copy(update={"g": [
            (name, uncertain, [i for i in ids if i not in ignored_lines],
             [fact for fact in facts if fact[1] not in excluded_lines])
            for name, uncertain, ids, facts in response.g
            if not ids or any(i not in excluded_lines for i in ids)
        ]})
    scan = response if isinstance(response, WholeDocumentResponse) else response.expand()
    if not scan.complete:
        raise IncompleteScan(INCOMPLETE_SCAN)
    groups, evidence, failures = ground_groups(
        text, chunk.lines, scan, start_offset=chunk.start, allow_unsupported_names=True,
    )
    if failures:
        raise InvalidCompletion(f"Invalid completion: {failures} invalid source references")
    if isinstance(response, CatalogChunkResponse):
        attach_catalog_facts(
            response, groups, {item.evidence_id: item for item in evidence},
            recover_fields=chunk.prompt_version in CATALOG_SPLIT_VERSIONS,
        )
    return {"groups": groups, "evidence": [item.model_dump(mode="json") for item in evidence]}


def scan_chunk(
    repo: Any, client: GatewayClient, extraction_id: int, text: str, chunk: Chunk,
    split_depth: int, max_prompt_chars: int, record_attempt: Callable[[str, dict[str, Any]], None],
) -> dict[str, Any]:
    checkpoint = repo.get_extraction_request(extraction_id, chunk.key)
    if checkpoint and checkpoint["status"] == "successful":
        return checkpoint["result"]
    saved_failure = (checkpoint or {}).get("result") or {}
    saved_limit = saved_failure.get("max_prompt_chars")
    if saved_limit is not None:
        max_prompt_chars = min(max_prompt_chars, saved_limit)
    if prompt_character_count(chunk.payload["messages"]) > max_prompt_chars:
        raise RequestTooLarge("Prompt exceeds the gateway character limit", max_prompt_chars=max_prompt_chars)
    current = chunk.prompt_version in CATALOG_SPLIT_VERSIONS
    if current and saved_failure.get("reason") in {"output_truncated", "invalid_scan"} and split_depth < CATALOG_SPLIT_DEPTH:
        raise SavedSplitRequired(checkpoint.get("error") or "Saved incomplete scan")
    if not current and checkpoint and checkpoint.get("error") == INCOMPLETE_SCAN and split_depth < 2:
        raise SavedSplitRequired(INCOMPLETE_SCAN)
    if not current and saved_failure.get("reason") == "output_truncated":
        if split_depth < 2:
            raise SavedSplitRequired("Saved output truncation")
        if chunk.prompt_version != LEGACY_PROMPT_VERSION:
            raise OutputTruncated("Compact completion reached its output limit")
        chunk = make_chunk(text, chunk.start, chunk.end, max_prompt_chars, prompt_version=CANDIDATE_PROMPT_VERSION)
        checkpoint = repo.get_extraction_request(extraction_id, chunk.key)
        if checkpoint and checkpoint["status"] == "successful":
            return checkpoint["result"]
        if checkpoint and (checkpoint["result"] or {}).get("reason") == "output_truncated":
            raise OutputTruncated("Compact completion reached its output limit")

    attempts = (checkpoint or {}).get("attempts") or []
    last = attempts[-1] if attempts else {}
    # A worker can exit after committing the HTTP response but before committing its grounded result.
    interrupted_after_response = (
        checkpoint and checkpoint.get("status") == "running" and checkpoint.get("request") == chunk.payload
        and last.get("http_status") == 200 and last.get("error") is None
        and (last.get("response") or {}).get("model") == chunk.payload["model"]
    )
    if interrupted_after_response or (checkpoint and (checkpoint.get("error") or "").endswith(
        (" invalid source references", " invalid source references or names")
    )):
        response = saved_completion(last, "stop", FORMATS[chunk.prompt_version][0]) if last else None
        if response is not None:
            try:
                result = ground_chunk(text, chunk, response)
            except InvalidCompletion:
                pass
            else:
                repo.finish_extraction_request(extraction_id, chunk.key, result, None)
                return result

    repo.begin_extraction_request(extraction_id, chunk.key, chunk.payload)
    try:
        response_model = FORMATS[chunk.prompt_version][0]
        response = client.complete(
            chunk.payload, response_model, lambda attempt: record_attempt(chunk.key, attempt),
            lambda _: None, single_attempt=True,
        )
        result = ground_chunk(text, chunk, response)
    except InferenceError as exc:
        result = None
        if isinstance(exc, OutputTruncated):
            result = {"reason": "output_truncated"}
        elif current and isinstance(exc, InvalidCompletion):
            result = {"reason": "invalid_scan"}
        elif isinstance(exc, RequestTooLarge) and exc.max_prompt_chars is not None:
            result = {"max_prompt_chars": exc.max_prompt_chars}
        repo.finish_extraction_request(extraction_id, chunk.key, result, str(exc))
        raise
    repo.finish_extraction_request(extraction_id, chunk.key, result, None)
    return result


def run_catalog_extraction(
    repo: Any, client: GatewayClient, extraction_id: int, text: str, paper_id: str, source_sha256: str,
    *, prompt_version: str = PROMPT_VERSION,
) -> PublicationExtraction:
    started = time.monotonic()
    max_prompt_chars = MAX_PROMPT_CHARS
    compact = prompt_version != LEGACY_PROMPT_VERSION
    initial = plan_chunks(text, 0, len(text), max_prompt_chars, prompt_version=prompt_version)
    pending = deque((chunk, 0) for chunk in initial)
    groups, evidence, errors = [], [], []
    attempts = completed = covered = 0
    planned_chunks = call_limit = len(initial)

    def record_attempt(key: str, attempt: dict[str, Any]) -> None:
        nonlocal attempts
        repo.record_extraction_attempt(extraction_id, key, attempt)
        attempts += 1

    def progress() -> None:
        repo.record_extraction_progress(extraction_id, {
            "phase": "catalog", "attempts_this_invocation": attempts, "call_limit": call_limit,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "time_limit_seconds": config.LLM_TIMEOUT_SEC * call_limit,
            "planned_chunks": planned_chunks, "completed_chunks": completed,
            "max_prompt_characters": max_prompt_chars,
            "calls_by_phase": {"catalog": attempts},
        })

    progress()
    while pending:
        chunk, split_depth = pending.popleft()
        if not chunk.payload:
            covered += chunk.end - chunk.start
            planned_chunks -= 1
            call_limit -= 1
            progress()
            continue
        attempts_before = attempts
        try:
            result = scan_chunk(repo, client, extraction_id, text, chunk, split_depth, max_prompt_chars, record_attempt)
        except RequestTooLarge as exc:
            limit = exc.max_prompt_chars
            if limit is None or limit >= prompt_character_count(chunk.payload["messages"]):
                errors.append(f"characters {chunk.start}–{chunk.end}: {exc}")
                continue
            max_prompt_chars = min(max_prompt_chars, limit)
            try:
                replacements = plan_chunks(text, chunk.start, chunk.end, limit, prompt_version=chunk.prompt_version)
            except RequestTooLarge:
                errors.append(f"characters {chunk.start}–{chunk.end}: gateway limit of {max_prompt_chars} characters is too small")
                continue
            pending.extendleft((part, split_depth) for part in reversed(replacements))
            planned_chunks += len(replacements) - 1
            call_limit += len(replacements) - int(attempts == attempts_before)
        except (OutputTruncated, InvalidCompletion) as exc:
            if prompt_version in CATALOG_SPLIT_VERSIONS:
                can_split = split_depth < CATALOG_SPLIT_DEPTH
            else:
                can_split = isinstance(exc, (OutputTruncated, IncompleteScan)) and (
                    split_depth == 0 or isinstance(exc, SavedSplitRequired)
                )
            if can_split and chunk.end - chunk.start > 1:
                midpoint = preferred_end(text, chunk.start, (chunk.start + chunk.end) // 2)
                try:
                    children = [
                        child for start, end in ((chunk.start, midpoint), (midpoint, chunk.end))
                        for child in plan_chunks(text, start, end, max_prompt_chars, prompt_version=chunk.prompt_version)
                    ]
                except RequestTooLarge as size_error:
                    errors.append(f"characters {chunk.start}–{chunk.end}: {size_error}")
                    continue
                pending.extendleft((child, split_depth + 1) for child in reversed(children))
                planned_chunks += len(children) - 1
                call_limit += len(children) - int(attempts == attempts_before)
            else:
                errors.append(f"characters {chunk.start}–{chunk.end}: {exc}")
        except InferenceUnavailable:
            raise
        except InferenceError as exc:
            errors.append(f"characters {chunk.start}–{chunk.end}: {exc}")
        else:
            for group in result["groups"]:
                groups.append({**group, "group_id": f"d{len(groups) + 1}", "chunk": chunk.key})
            evidence.extend(result["evidence"])
            completed += 1
            covered += chunk.end - chunk.start
        finally:
            progress()

    if errors:
        raise ValueError(f"Catalog extraction incomplete. {len(errors)} of {planned_chunks} chunks failed. {errors[0]}")
    if covered != len(text):
        raise ValueError("Catalog extraction incomplete. Source coverage has a gap.")
    if prompt_version in CATALOG_SPLIT_VERSIONS:
        reconcile_catalog_identity(text, groups, evidence)
    if prompt_version == PROMPT_VERSION:
        groups = [
            group for group in groups
            if not group["bibliography_only"] or (
                group["name"] and not group["identity_uncertain"]
                and any(group["identity"].get(field, {}).get("state") == "reported"
                        for field in ("identifiers", "urls"))
            )
        ]
    blocked = refine_candidates(text, groups, evidence) if compact else set()
    result = publication_result({
        "status": "complete", "paper_id": paper_id, "source_sha256": source_sha256,
        "source_characters": len(text), "groups": groups, "evidence": evidence,
    }, prompt_version=prompt_version, validated_blocks=completed)
    if compact:
        result.datasets = consolidate(result.datasets, blocked)
    return result
