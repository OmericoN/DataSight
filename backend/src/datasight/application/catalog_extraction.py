"""Plan budgeted source chunks and extract grounded dataset candidates."""

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import StrictBool, StrictInt

from datasight import config
from datasight.application.extraction_engine import consolidate
from datasight.application.catalog_features import CATALOG_PROMPT, ROLE_PROMPT, CatalogChunkResponse, CatalogUseChunkResponse, attach_catalog_facts, has_deposit_marker
from datasight.application.catalog_candidate_resolution import refine_candidates
from datasight.application.whole_document_extraction import (
    CandidateGroup,
    LOCATE_GROUPS,
    WholeDocumentResponse,
    ground_groups,
    publication_result,
    saved_completion,
)
from datasight.domain.extraction import PublicationExtraction, StrictModel
from datasight.infrastructure.llm.client import (
    MAX_PROMPT_CHARS, GatewayClient, InferenceError, InferenceUnavailable, InvalidCompletion,
    OutputTruncated, RequestTooLarge, payload_for, prompt_character_count,
)

PROMPT_VERSION = "catalog-v4"
PREVIOUS_CATALOG_V4 = "fast-catalog-v4"
CATALOG_V3 = "fast-catalog-v3"
CANDIDATE_PROMPT_VERSION = "fast-candidates-v2"
LEGACY_PROMPT_VERSION = "fast-candidates-v1"
CATALOG_PROMPT_VERSIONS = (PROMPT_VERSION, PREVIOUS_CATALOG_V4, CATALOG_V3, CANDIDATE_PROMPT_VERSION, LEGACY_PROMPT_VERSION)
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
    PROMPT_VERSION: (CatalogUseChunkResponse, ROLE_PROMPT, ":catalog-use"),
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

    @property
    def key(self) -> str:
        prefix = "catalog" if self.prompt_version == PROMPT_VERSION else "fast"
        return f"{prefix}:{self.start}:{self.end}" + FORMATS[self.prompt_version][2]


def make_chunk(text: str, start: int, end: int, max_prompt_chars: int, *, prompt_version: str = LEGACY_PROMPT_VERSION) -> Chunk:
    lines = text[start:end].splitlines(keepends=True)
    model, instruction, _ = FORMATS[prompt_version]
    data: dict[str, Any] = {"lines": list(enumerate(lines, 1))}
    if issubclass(model, CatalogChunkResponse):
        deposits = [i for i, line in data["lines"] if line.startswith("- **[")
                    and ("DOI:" in line or "URL:" in line) and has_deposit_marker(line)]
        if deposits:
            data["possible_data_deposit_lines"] = deposits
            instruction += "\nCheck each possible_data_deposit_lines entry. Extract confirmed data citations with DOI/URL, title, creators and release year. Skip software and articles."
    payload = payload_for(
        instruction, data, model,
        max_prompt_chars=max_prompt_chars,
    )
    return Chunk(start, end, lines, payload, prompt_version)


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
    if stop - start <= max_prompt_chars:
        try:
            return [make_chunk(text, start, stop, max_prompt_chars, prompt_version=prompt_version)]
        except RequestTooLarge:
            pass

    chunks = []
    while start < stop:
        low, high = start + 1, min(stop, start + max_prompt_chars)
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
        response = response.model_copy(update={"g": [
            (name, uncertain, [i for i in ids if i not in blank_lines], facts)
            for name, uncertain, ids, facts in response.g
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
        attach_catalog_facts(response, groups, {item.evidence_id: item for item in evidence})
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
    if checkpoint and checkpoint.get("error") == INCOMPLETE_SCAN and split_depth < 2:
        raise SavedSplitRequired(INCOMPLETE_SCAN)
    if saved_failure.get("reason") == "output_truncated":
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

    if checkpoint and (checkpoint.get("error") or "").endswith((" invalid source references", " invalid source references or names")):
        attempts = checkpoint.get("attempts") or []
        response = saved_completion(attempts[-1], "stop", FORMATS[chunk.prompt_version][0]) if attempts else None
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
                replacements = plan_chunks(text, chunk.start, chunk.end, max_prompt_chars, prompt_version=chunk.prompt_version)
            except RequestTooLarge:
                errors.append(f"characters {chunk.start}–{chunk.end}: gateway limit of {max_prompt_chars} characters is too small")
                continue
            pending.extendleft((part, split_depth) for part in reversed(replacements))
            planned_chunks += len(replacements) - 1
            call_limit += len(replacements) - int(attempts == attempts_before)
        except (OutputTruncated, IncompleteScan) as exc:
            if (split_depth == 0 or isinstance(exc, SavedSplitRequired)) and chunk.end - chunk.start > 1:
                midpoint = preferred_end(text, chunk.start, (chunk.start + chunk.end) // 2)
                left = make_chunk(text, chunk.start, midpoint, max_prompt_chars, prompt_version=chunk.prompt_version)
                right = make_chunk(text, midpoint, chunk.end, max_prompt_chars, prompt_version=chunk.prompt_version)
                pending.extendleft(((right, split_depth + 1), (left, split_depth + 1)))
                planned_chunks += 1
                call_limit += 2 - int(attempts == attempts_before)
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
    blocked = refine_candidates(text, groups, evidence) if compact else set()
    result = publication_result({
        "status": "complete", "paper_id": paper_id, "source_sha256": source_sha256,
        "source_characters": len(text), "groups": groups, "evidence": evidence,
    }, prompt_version=prompt_version, validated_blocks=completed)
    if compact:
        result.datasets = consolidate(result.datasets, blocked)
    return result
