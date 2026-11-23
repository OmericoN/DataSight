"""Transparent, recall-first rules-v3 candidate window detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from datasight.domain.schemas import MentionCandidate, UMDatasetRecord

DETECTOR_VERSION = "rules-v3"
SCREENING_VERSION = "dataset-use-model-v1"
SECTION_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>.+)$", re.MULTILINE)
PARAGRAPH_RE = re.compile(r"(?m)(?P<paragraph>\S(?:.*?\S)?)(?=\n[ \t]*\n|\s*\Z)", re.DOTALL)

TRIGGER_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (
        "accession",
        r"\b(?:GSE\d{3,}|GSM\d{3,}|PRJNA\d{3,}|PRJEB\d{3,}|SRP\d{3,}|SRR\d{3,}|ERP\d{3,}|ERR\d{3,}|PXD\d{3,}|phs\d{6}(?:\.v\d+\.p\d+)?|ICPSR\s*\d{3,})\b",
        "strong",
    ),
    (
        "repository_url",
        r"https?://[^\s<>{}\[\]]*(?:zenodo|dataverse|dryad|figshare|ncbi\.nlm\.nih\.gov/(?:geo|sra)|ebi\.ac\.uk/pride|dbgap|pangaea|icpsr)[^\s<>{}\[\]]*",
        "strong",
    ),
    (
        "repository_name",
        r"\b(?:Zenodo|Dataverse|Dryad|Figshare|Gene Expression Omnibus|GEO|Sequence Read Archive|SRA|PRIDE|dbGaP|PANGAEA|ICPSR)\b",
        "medium",
    ),
    (
        "dataset_doi",
        r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b",
        "medium",
    ),
    (
        "data_action",
        r"\b(?:data|dataset|records?|samples?|responses?)\s+(?:were\s+|was\s+)?(?:used|accessed|obtained|retrieved|downloaded|collected|gathered|deposited|released|shared|available)\b|\b(?:used|accessed|obtained|retrieved|downloaded|collected|gathered|deposited)\s+(?:the\s+)?(?:data|dataset|records?|samples?|survey responses?)\b|\b(?:we|the authors?)\s+(?:used|accessed|obtained|retrieved|downloaded|collected|gathered|deposited)\b",
        "medium",
    ),
    (
        "availability_phrase",
        r"\b(?:data availability|data are available|data is available|available (?:from|at|in|through)|deposited (?:in|at)|accessed (?:from|through)|retrieved from)\b",
        "medium",
    ),
    (
        "dataset_term",
        r"\b(?:dataset|data set|corpus|cohort|registry|biobank|benchmark|survey data|databank|data repository)\b",
        "broad",
    ),
)
COMPILED_TRIGGERS = tuple((kind, re.compile(pattern, re.I), tier) for kind, pattern, tier in TRIGGER_PATTERNS)
NAMED_DATASET_PATTERNS = (
    re.compile(r"\b(?P<name>[A-Z][A-Za-z0-9&'()./_ -]{2,100}?)\s+(?:dataset|database|databank|registry|biobank|corpus|cohort)\b"),
    re.compile(r"\b(?:dataset|database|databank|registry|biobank|corpus|cohort)\s+(?:called|named|from|provided by)\s+(?P<name>[A-Z][A-Za-z0-9&'()./_ -]{2,100})\b"),
)
TIER_RANK = {"broad": 0, "medium": 1, "strong": 2}
TIER_SCORE = {"broad": 0.35, "medium": 0.65, "strong": 0.9}


@dataclass(frozen=True)
class _Trigger:
    start: int
    end: int
    kind: str
    text: str
    tier: str
    catalog_name: str | None = None


@dataclass
class _Window:
    start: int
    end: int
    triggers: list[_Trigger]


def detect_dataset_candidates(
    publication_id: str,
    markdown: str,
    min_score: float = 0.0,
    *,
    render_sha256: str | None = None,
    catalog_records: Iterable[UMDatasetRecord | dict[str, Any]] | None = None,
) -> list[MentionCandidate]:
    """Return all strong, medium, and broad trigger windows in recall-first mode.

    ``score`` remains only as a backwards-compatible tier encoding. It is not a
    probability and must not be interpreted as calibrated confidence.
    """
    sections = _section_spans(markdown)
    catalog_patterns = _catalog_patterns(catalog_records or [])
    windows: list[_Window] = []

    for paragraph_match in PARAGRAPH_RE.finditer(markdown):
        paragraph = paragraph_match.group("paragraph")
        if paragraph.lstrip().startswith("#"):
            continue
        paragraph_start, paragraph_end = paragraph_match.span("paragraph")
        triggers = _find_triggers(paragraph, paragraph_start, catalog_patterns)
        if not triggers:
            continue
        local_windows = [_window_for_trigger(markdown, paragraph_start, paragraph_end, trigger) for trigger in triggers]
        windows.extend(_merge_windows(local_windows))

    candidates: list[MentionCandidate] = []
    seen: set[tuple[int, int]] = set()
    for window in windows:
        start, end = _trim_offsets(markdown, window.start, window.end)
        if start >= end or (start, end) in seen:
            continue
        seen.add((start, end))
        evidence = markdown[start:end]
        primary = max(window.triggers, key=lambda trigger: TIER_RANK[trigger.tier])
        tier = _tier_for_window(window.triggers)
        score = TIER_SCORE[tier]
        if score < min_score:
            continue
        section = _section_for_offset(sections, start)
        candidates.append(
            MentionCandidate(
                publication_id=publication_id,
                dataset_name=_dataset_name(evidence, window.triggers),
                evidence_text=evidence,
                section_heading=section,
                standardized_section=standardize_section(section),
                char_start=start,
                char_end=end,
                score=score,
                source="rule",
                evidence_tier=tier,  # type: ignore[arg-type]
                trigger_type=primary.kind,
                trigger_text=primary.text,
                triggers=[
                    {"type": trigger.kind, "text": trigger.text, "tier": trigger.tier}
                    for trigger in sorted(window.triggers, key=lambda trigger: (trigger.start, trigger.end))
                ],
                detector_version=DETECTOR_VERSION,
                render_sha256=render_sha256,
            )
        )
    return candidates


def _catalog_patterns(
    records: Iterable[UMDatasetRecord | dict[str, Any]],
) -> list[tuple[re.Pattern[str], str]]:
    patterns: list[tuple[re.Pattern[str], str]] = []
    seen: set[str] = set()
    for record in records:
        value = record.model_dump(mode="python") if isinstance(record, UMDatasetRecord) else dict(record)
        names = [value.get("title"), *(value.get("aliases") or []), value.get("doi"), value.get("url")]
        for name in names:
            cleaned = str(name or "").strip()
            key = cleaned.casefold()
            if len(cleaned) < 4 or key in seen:
                continue
            seen.add(key)
            patterns.append((re.compile(rf"(?<!\w){re.escape(cleaned)}(?!\w)", re.I), str(value.get("title") or cleaned)))
    return patterns


def _find_triggers(
    paragraph: str,
    paragraph_start: int,
    catalog_patterns: list[tuple[re.Pattern[str], str]],
) -> list[_Trigger]:
    triggers: list[_Trigger] = []
    has_data_context = bool(re.search(r"\b(?:data|dataset|corpus|cohort|registry|biobank|repository|accession)\b", paragraph, re.I))
    for kind, pattern, tier in COMPILED_TRIGGERS:
        if kind == "dataset_doi" and not has_data_context:
            continue
        for match in pattern.finditer(paragraph):
            triggers.append(
                _Trigger(paragraph_start + match.start(), paragraph_start + match.end(), kind, match.group(0), tier)
            )
    for pattern, catalog_name in catalog_patterns:
        for match in pattern.finditer(paragraph):
            triggers.append(
                _Trigger(
                    paragraph_start + match.start(),
                    paragraph_start + match.end(),
                    "um_catalog_exact",
                    match.group(0),
                    "strong",
                    catalog_name,
                )
            )
    return triggers


def _window_for_trigger(markdown: str, paragraph_start: int, paragraph_end: int, trigger: _Trigger) -> _Window:
    if paragraph_end - paragraph_start <= 650:
        return _Window(paragraph_start, paragraph_end, [trigger])
    center = (trigger.start + trigger.end) // 2
    start = max(paragraph_start, center - 250)
    end = min(paragraph_end, center + 250)
    start = _expand_left(markdown, start, paragraph_start)
    end = _expand_right(markdown, end, paragraph_end)
    return _Window(start, end, [trigger])


def _expand_left(text: str, start: int, floor: int) -> int:
    boundary = max(floor, start - 100)
    candidates = [text.rfind(mark, boundary, start) for mark in (". ", "? ", "! ", "; ", "\n")]
    found = max(candidates)
    return found + 1 if found >= boundary else start


def _expand_right(text: str, end: int, ceiling: int) -> int:
    candidates = [position for mark in (". ", "? ", "! ", "; ", "\n") if (position := text.find(mark, end, min(ceiling, end + 100))) >= 0]
    return min(candidates) + 1 if candidates else end


def _merge_windows(windows: list[_Window]) -> list[_Window]:
    merged: list[_Window] = []
    for window in sorted(windows, key=lambda item: (item.start, item.end)):
        if merged and window.start <= merged[-1].end + 40:
            merged[-1].end = max(merged[-1].end, window.end)
            merged[-1].triggers.extend(window.triggers)
        else:
            merged.append(_Window(window.start, window.end, list(window.triggers)))
    return merged


def _tier_for_window(triggers: list[_Trigger]) -> str:
    strongest = max(triggers, key=lambda trigger: TIER_RANK[trigger.tier]).tier
    kinds = {trigger.kind for trigger in triggers}
    if strongest == "medium" and "dataset_term" in kinds and kinds & {"data_action", "availability_phrase", "repository_name", "dataset_doi"}:
        return "strong"
    if strongest == "broad" and len(triggers) > 1:
        return "medium"
    return strongest


def _dataset_name(evidence: str, triggers: list[_Trigger]) -> str:
    for trigger in triggers:
        if trigger.catalog_name:
            return trigger.catalog_name
    for pattern in NAMED_DATASET_PATTERNS:
        match = pattern.search(evidence)
        if match:
            name = re.sub(r"\s+", " ", match.group("name")).strip(" ,.;:()[]")
            name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I)
            if len(name) > 2:
                return name
    accession = next((trigger.text for trigger in triggers if trigger.kind == "accession"), None)
    if accession:
        return accession
    if any(trigger.kind in {"data_action", "availability_phrase"} for trigger in triggers):
        return "Author-described dataset"
    return "Unresolved dataset mention"


def _trim_offsets(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def standardize_section(section_heading: str | None) -> str | None:
    if not section_heading:
        return None
    text = section_heading.casefold()
    if "availability" in text:
        return "Data Availability"
    if any(token in text for token in ("method", "material", "data", "source", "cohort")):
        return "Methodology"
    if "abstract" in text:
        return "Abstract"
    if "result" in text:
        return "Results"
    if "discussion" in text:
        return "Discussion"
    if "conclusion" in text:
        return "Conclusion"
    if "introduction" in text:
        return "Introduction"
    if any(token in text for token in ("appendix", "supplement")):
        return "Supplementary Material"
    return "Other"


def _section_spans(markdown: str) -> list[tuple[int, str]]:
    return [(match.start(), match.group("title").strip()) for match in SECTION_RE.finditer(markdown)]


def _section_for_offset(section_spans: list[tuple[int, str]], offset: int) -> str | None:
    current: str | None = None
    for start, title in section_spans:
        if start > offset:
            break
        current = title
    return current
