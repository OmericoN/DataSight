"""Whole-document candidate extraction from immutable publication snapshots."""

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import StrictBool, StrictInt, ValidationError

from datasight import config
from datasight.application.extraction_engine import INSTRUCTIONS, Block, evidence_for
from datasight.domain.extraction import (
    DatasetEntry, Evidence, EvidenceValue, IdentityFeatures, LocatedPassage, Observed, Occurrence,
    PublicationExtraction, StrictModel, UseFeatures,
)
from datasight.infrastructure.llm.client import (
    CONTEXT_TOKENS,
    MAX_PROMPT_CHARS,
    MAX_TOKENS,
    TEMPLATE_RESERVE,
    GatewayClient,
    InferenceError,
    InferenceUnavailable,
    InvalidCompletion,
    OutputTruncated,
    RequestTooLarge,
    payload_for,
)
from datasight.infrastructure.llm.tokenizer import TOKENIZER_REVISION, TOKENIZER_SHA256

PROMPT_VERSION = "whole-document-stage-a-v1"
LOCATE_GROUPS = INSTRUCTIONS + """Read the entire publication in numbered lines [line_id, text].
Return every dataset candidate, including implicit mentions and unnamed data collected
or analyzed in the study. Each group represents one proposed dataset identity. Group
references only when the source makes their shared identity clear. Keep explicitly distinct
datasets and versions separate, even if they share a name. Keep unresolved mentions separate.
Set identity_uncertain when the identity or whether the passage refers to a dataset is unclear.
Use a name copied exactly from one of that group's selected lines, or null if unnamed
or unresolved. Return only line IDs as evidence, with no quotations or summaries.
Select bibliography-only entries only when they explicitly refer to a dataset.
An article title, article DOI, repository name or citation alone does not establish a dataset.
Set complete to true only if you finished reviewing the entire publication and included
all candidates you identified. If you could only return a subset, set complete to false.
Return empty groups if there is no dataset evidence. Use compact JSON.
"""


class CandidateGroup(StrictModel):
    name: str | None
    identity_uncertain: StrictBool
    line_ids: list[StrictInt]


class WholeDocumentResponse(StrictModel):
    complete: StrictBool
    groups: list[CandidateGroup]


class SourceError(ValueError):
    """A frozen snapshot could not be used."""


def whole_document_settings() -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "model": config.LLM_MODEL,
        "model_revision": config.LLM_MODEL_REVISION,
        "tokenizer_revision": TOKENIZER_REVISION,
        "tokenizer_sha256": TOKENIZER_SHA256,
        "context_tokens": CONTEXT_TOKENS,
        "max_prompt_characters": MAX_PROMPT_CHARS,
        "max_tokens": MAX_TOKENS,
        "template_reserve_tokens": TEMPLATE_RESERVE,
        "max_http_attempts": 1,
        "timeout_seconds": config.LLM_TIMEOUT_SEC,
        "enable_thinking": False,
    }


def read_frozen_source(source_path: str | None, source_sha256: str | None) -> str:
    if not source_path:
        raise SourceError("source_missing")
    try:
        raw = Path(source_path).read_bytes()
    except FileNotFoundError as exc:
        raise SourceError("source_missing") from exc
    except (OSError, ValueError) as exc:
        raise SourceError("source_unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != source_sha256:
        raise SourceError("source_hash_mismatch")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise SourceError("source_invalid_utf8") from exc
    if not text.strip():
        raise SourceError("source_empty")
    return text


def ground_groups(
    text: str, lines: list[str], response: WholeDocumentResponse, *, start_offset: int = 0,
    allow_unsupported_names: bool = False,
) -> tuple[list[dict[str, Any]], list[Evidence], int]:
    spans = []
    start = start_offset
    for line in lines:
        end = start + len(line.removesuffix("\n").removesuffix("\r"))
        spans.append(Block(start, end))
        start += len(line)

    groups = []
    evidence_by_line: dict[int, Evidence] = {}
    failures = 0
    for group in response.groups:
        if not group.line_ids:
            failures += 1
        passages = []
        for line_id in dict.fromkeys(group.line_ids):
            if not 1 <= line_id <= len(spans) or not lines[line_id - 1].strip():
                failures += 1
                continue
            if line_id not in evidence_by_line:
                block = spans[line_id - 1]
                passage = LocatedPassage(
                    block_id=block.block_id, quote=text[block.start:block.end],
                    quote_occurrence=0, dataset_name=None, kind="reference",
                )
                evidence_by_line[line_id] = evidence_for(text, block, passage)
            passages.append(evidence_by_line[line_id])
        if not passages:
            continue
        name, uncertain = group.name, group.identity_uncertain
        if name is not None and not (name.strip() and any(name in item.quote for item in passages)):
            if not allow_unsupported_names:
                failures += 1
            name, uncertain = None, True
        groups.append({
            "group_id": f"d{len(groups) + 1}",
            "name": name,
            "identity_uncertain": uncertain,
            "evidence_ids": [item.evidence_id for item in passages],
            "bibliography_only": all(item.placement == "bibliography" for item in passages),
        })
    return groups, sorted(evidence_by_line.values(), key=lambda item: item.char_start), failures


ResponseT = TypeVar('ResponseT', bound=StrictModel)


def saved_completion(attempt: dict[str, Any], finish_reason: str, model: type[ResponseT]) -> ResponseT | None:
    try:
        choice = attempt["response"]["choices"][0]
        if choice["finish_reason"] == finish_reason:
            return model.model_validate_json(choice["message"]["content"])
    except (IndexError, KeyError, TypeError, ValidationError):
        return None
    return None


def extract_whole_document(
    client: GatewayClient,
    snapshot: dict[str, Any],
    *,
    record_attempt: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []
    reasons = []
    groups, evidence, failures = [], [], 0
    source_characters = 0
    try:
        text = read_frozen_source(snapshot["source_path"], snapshot["source_sha256"])
        source_characters = len(text)
        lines = text.splitlines(keepends=True)
        payload = payload_for(LOCATE_GROUPS, {"lines": list(enumerate(lines, 1))}, WholeDocumentResponse)

        def capture_attempt(attempt: dict[str, Any]) -> None:
            attempts.append(attempt)
            if record_attempt is not None:
                record_attempt(payload, attempt)

        try:
            response = client.complete(
                payload, WholeDocumentResponse, capture_attempt, lambda _: None, single_attempt=True,
            )
        except OutputTruncated:
            reasons.append("output_truncated")
            response = saved_completion(attempts[-1], "length", WholeDocumentResponse)
    except SourceError as exc:
        reasons.append(str(exc))
    except RequestTooLarge:
        reasons.append("gateway_rejected_size" if attempts else "over_budget")
    except InvalidCompletion:
        reasons.append("invalid_completion")
    except InferenceUnavailable:
        reasons.append("gateway_unavailable")
    except InferenceError:
        reasons.append("gateway_failure")
    except ValueError:
        reasons.append("tokenizer_unavailable")
    else:
        if response is not None:
            if not response.complete:
                reasons.append("model_incomplete")
            groups, evidence, failures = ground_groups(text, lines, response)
            if failures:
                reasons.append("ungrounded_evidence")

    attempt = attempts[0] if attempts else {}
    body = attempt.get("response")
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    tokens: dict[str, int | None] = {"prompt_tokens": 0, "completion_tokens": 0}
    if attempts:
        for key in tokens:
            count = usage.get(key)
            tokens[key] = count if type(count) is int and count >= 0 else None
    return {
        "paper_id": snapshot["paper_id"],
        "source_sha256": snapshot["source_sha256"],
        "source_characters": source_characters,
        "status": "incomplete" if reasons else "complete",
        "reasons": reasons,
        "http_status": attempt.get("http_status"),
        "groups": groups,
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "metrics": {
            "http_requests": len(attempts),
            **tokens,
            "attempts_missing_usage": int(None in tokens.values()),
            "extraction_seconds": round(time.monotonic() - started, 3),
            "grounded_reference_count": sum(len(group["evidence_ids"]) for group in groups),
            "distinct_identity_count": len(groups),
            "uncertain_identity_count": sum(group["identity_uncertain"] for group in groups),
            "grounding_failures": failures,
            "output_truncations": int("output_truncated" in reasons),
        },
    }


def current_paper_use(roles: dict[str, list[str]], evidence_ids: list[str]) -> UseFeatures:
    values = []
    subject_ids = set()
    for role, references in roles.items():
        matching = [eid for eid in evidence_ids if eid in references]
        if matching:
            values.append({"value": role, "evidence_ids": matching})
            subject_ids.update(matching)
    if not values:
        return UseFeatures()
    return UseFeatures.model_validate({
        "roles": {"state": "reported", "values": values},
        "claim_subject": {"state": "reported", "values": [
            {"value": "current_publication", "evidence_ids": sorted(subject_ids)},
        ]},
    })


def publication_result(
    report: dict[str, Any], *, prompt_version: str = PROMPT_VERSION, validated_blocks: int = 1,
) -> PublicationExtraction:
    if report["status"] != "complete":
        raise ValueError("Only complete whole-document results can be published")

    evidence = [Evidence.model_validate(item) for item in report["evidence"]]
    by_id = {item.evidence_id: item for item in evidence}
    datasets, unresolved = [], set()
    for group in report["groups"]:
        group_id, name, ids = group["group_id"], group["name"], group["evidence_ids"]
        roles = group.get("roles", {})
        entry = DatasetEntry(
            dataset_local_id=group_id,
            display_name=name or "Unnamed dataset",
            use=current_paper_use(roles, ids),
            identity=IdentityFeatures.model_validate(group.get("identity", {})),
            review_reasons=list(group.get("review_reasons", [])),
            occurrences=[
                Occurrence(occurrence_id=f"{group_id}-o{index}", evidence_ids=[evidence_id],
                           use=current_paper_use(roles, [evidence_id]),
                           bibliography_only=by_id[evidence_id].placement == "bibliography")
                for index, evidence_id in enumerate(ids, 1)
            ],
        )
        if name is not None:
            entry.identity.name = Observed[str](state="reported", values=[
                EvidenceValue[str](value=name, evidence_ids=[item for item in ids if name in by_id[item].quote]),
            ])
        if group["identity_uncertain"]:
            entry.review_reasons.append("Dataset identity is uncertain")
            unresolved.update(ids)
        datasets.append(entry)
    return PublicationExtraction(
        prompt_version=prompt_version,
        paper_id=report["paper_id"], source_sha256=report["source_sha256"],
        evidence=evidence, datasets=datasets, unresolved_mentions=sorted(unresolved),
        coverage={"source_characters": report["source_characters"],
                  "covered_characters": report["source_characters"], "validated_blocks": validated_blocks},
    )
