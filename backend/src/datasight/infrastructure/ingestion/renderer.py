"""TEI XML to profiled Markdown rendering with artifact lineage."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import lxml.etree as etree

from datasight.config import MARKDOWN_DIR
from datasight.domain.results import RENDERER_VERSION as RENDERER_VERSION, RenderResult
from datasight.infrastructure.ingestion.converter import validate_tei
from datasight.infrastructure.ingestion.file_integrity import sha256_file

TEI = "http://www.tei-c.org/ns/1.0"
XML = "http://www.w3.org/XML/1998/namespace"
NS = {"t": TEI}
LEGACY_RENDERER_VERSION = "tei-renderer-v6"
PREVIOUS_RENDERER_VERSION = "tei-renderer-v7"
RENDERER_VERSIONS = (RENDERER_VERSION, PREVIOUS_RENDERER_VERSION, LEGACY_RENDERER_VERSION)
UNPARSED_TABLE_HEADING = "## Unparsed appendix table material"
UNPARSED_TABLE_PREFIX = "> "
RenderProfile = Literal["full_body", "pruned"]
DEFAULT_RENDER_PROFILE: RenderProfile = "pruned"
logger = logging.getLogger(__name__)

LEGACY_PRUNED_SECTION_PATTERNS = (
    r"related.work",
    r"previous.work",
    r"prior.work",
    r"literature.review",
    r"discuss",
    r"conclusion",
    r"acknowledg",
    r"funding",
    r"conflict.of.interest",
    r"declaration",
    r"supplementar",
    r"appendix",
    r"author.contribution",
    r"abbreviation",
    r"ethical",
)
PRUNED_HEADINGS = re.compile(
    r"(?:related work|previous work|prior work|literature review|acknowledg[e]?ments?|"
    r"funding(?: information)?|competing interests?|conflicts? of interest|author contributions?|abbreviations)",
    re.I,
)
DELIMITED_LINKS = re.compile(r"\[(https?://[^\[\]]{1,2000})\]|<(https?://[^<>]{1,2000})>", re.I)
LINK_SEPARATOR = re.compile(r"[,;]\s*(?=https?://)", re.I)
DOI_HOSTS = {"doi.org", "dx.doi.org"}
DATASET_INVENTORY_CAPTION = re.compile(
    r"^Table\s+[A-Z]\.?\d+[a-z]?\s*[.:]\s*(?:Overview|List|Summary|Inventory)\s+of\b"
    r"[^.]{0,240}\bdatasets?\b[^.]{0,240}\b(?:training|evaluation|testing|validation|benchmarking)\b",
    re.I,
)


@dataclass
class _RenderState:
    profile: RenderProfile
    legacy: bool
    references: dict[str, dict[str, Any]]
    footnotes: dict[str, str]
    cited_refs: set[str] = field(default_factory=set)
    used_footnotes: dict[str, int] = field(default_factory=dict)
    source_sections: list[str] = field(default_factory=list)
    rendered_sections: list[str] = field(default_factory=list)
    figures: int = 0
    tables: int = 0
    warnings: list[str] = field(default_factory=list)


def _clean_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text or "")
    return re.sub(r"[ \t]+", " ", text).strip()


def _text(element: etree._Element) -> str:
    return _clean_text("".join(element.itertext()))


def _first_text(element: etree._Element, xpath: str) -> str:
    matches = element.xpath(xpath, namespaces=NS)
    return _text(matches[0]) if matches else ""


def _web_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        hostname = url.hostname or ""
    except ValueError:
        return False
    return (
        url.scheme in {"http", "https"}
        and "." in hostname.strip(".")
        and not any(char.isspace() for char in value)
    )


def _repair_delimited_links(markdown: str) -> str:
    """Join PDF word fragments inside an explicitly delimited list of URLs."""
    def replace(match: re.Match[str]) -> str:
        source = match.group(1) or match.group(2)
        if "\n\n" in source:
            return match.group(0)
        links = []
        for raw in LINK_SEPARATOR.split(source):
            parts = raw.strip().split()
            joined = "".join(parts)
            if not _web_url(joined):
                return match.group(0)
            # An intact URL followed by ordinary words may be a prose link label.
            if len(parts) > 1 and _web_url(parts[0]) and not parts[0].endswith(("/", "+", "-", "_")):
                return match.group(0)
            if len(parts) > 2 and not urlsplit(joined).path:
                return match.group(0)
            links.append(joined)
        return match.group(0)[0] + ", ".join(links) + match.group(0)[-1]

    return DELIMITED_LINKS.sub(replace, markdown)


def _normalized_reference_links(dois: list[str], urls: list[str]) -> tuple[list[str], list[str]]:
    identifiers = list(dict.fromkeys(
        re.sub(r"^https?://(?:dx\.)?doi\.org/", "", "".join(value.split()), flags=re.I)
        for value in dois
    ))
    links = []
    for value in dict.fromkeys("".join(url.split()) for url in urls):
        if not _web_url(value):
            continue
        parsed = urlsplit(value)
        prefix = parsed.path.lstrip("/").casefold()
        if parsed.hostname in DOI_HOSTS and any(
            doi.casefold() == prefix or doi.casefold().startswith(prefix + ".") for doi in identifiers
        ):
            continue  # Prefer the complete structured DOI over a duplicate or truncated resolver link.
        links.append(value)
    return identifiers, links


def _parse_references(root: etree._Element, *, legacy: bool) -> dict[str, dict[str, Any]]:
    references: dict[str, dict[str, Any]] = {}
    for index, bib in enumerate(root.xpath("//t:listBibl/t:biblStruct | //t:listBibl/t:bibl", namespaces=NS), 1):
        xml_id = bib.get(f"{{{XML}}}id") or f"reference-{index}"
        authors = [
            _text(node)
            for node in bib.xpath(".//t:author/t:persName/t:surname", namespaces=NS)
            if _text(node)
        ]
        title = _first_text(bib, ".//t:analytic/t:title") or _first_text(bib, ".//t:monogr/t:title")
        year = ""
        for date in bib.xpath(".//t:date", namespaces=NS):
            match = re.search(r"\b(?:19|20)\d{2}\b", date.get("when", "") or _text(date))
            if match:
                year = match.group(0)
                break
        dois = [_text(node) for node in bib.xpath(".//t:idno[@type='DOI']", namespaces=NS) if _text(node)]
        urls = [node.get("target", "") for node in bib.xpath(".//t:ptr[@target]", namespaces=NS)]
        if legacy:
            dois, urls = dois[:1], urls[:1]
        else:
            urls.extend(node.get("target", "") for node in bib.xpath(".//t:ref[@target]", namespaces=NS))
            dois, urls = _normalized_reference_links(dois, urls)
        label = f"{authors[0]}, {year}" if authors else year or xml_id
        references[xml_id] = {
            "authors": authors,
            "title": title,
            "year": year,
            "dois": dois,
            "urls": urls,
            "label": label,
            "raw": " ".join(_clean_text(" ".join(bib.itertext())).split()),
            "list": bib.getparent(),
        }
    return references


def _parse_footnotes(root: etree._Element) -> dict[str, str]:
    return {
        note.get(f"{{{XML}}}id", ""): _text(note)
        for note in root.xpath("//t:note[@place='foot']", namespaces=NS)
        if note.get(f"{{{XML}}}id") and _text(note)
    }


def _render_children(element: etree._Element, state: _RenderState, depth: int) -> str:
    parts: list[str] = [element.text or ""]
    for child in element:
        parts.append(_render_element(child, state, depth))
        parts.append(child.tail or "")
    return "".join(parts)


def _render_table(element: etree._Element, state: _RenderState) -> str:
    state.tables += 1
    rows: list[list[str]] = []
    for row in element.xpath(".//t:row", namespaces=NS):
        cells = [_text(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        if cells:
            rows.append(cells)
    if not rows:
        text = _text(element)
        return f"\n\n{text}\n\n" if text else ""
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(padded[0]) + " |", "| " + " | ".join("---" for _ in range(width)) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n\n" + "\n".join(lines) + "\n\n"


def _render_element(element: etree._Element, state: _RenderState, depth: int = 0) -> str:
    if element.tag == etree.Comment or not isinstance(element.tag, str):
        return ""
    tag = etree.QName(element.tag).localname

    if tag == "listBibl":
        return ""  # References are rendered once with stable IDs below.

    if tag == "div":
        heads = element.xpath("t:head", namespaces=NS)
        heading = _text(heads[0]) if heads else ""
        if heading:
            state.source_sections.append(heading)
        if state.profile == "pruned" and heading:
            if state.legacy and any(re.search(pattern, heading, re.I) for pattern in LEGACY_PRUNED_SECTION_PATTERNS):
                return ""
            if not state.legacy and PRUNED_HEADINGS.fullmatch(heading.rstrip(".:")) and not element.findall("t:div", NS):
                return ""
        if heading:
            state.rendered_sections.append(heading)
        return _render_children(element, state, depth + 1)

    if tag == "head":
        heading = _text(element)
        number = element.get("n", "").strip()
        prefix = f"{number} " if number else ""
        return f"\n\n{'#' * min(depth + 1, 6)} {prefix}{heading}\n\n" if heading else ""

    if tag == "p":
        content = _clean_text(_render_children(element, state, depth))
        return f"\n\n{content}\n\n" if content else ""

    if tag == "ref":
        visible = _text(element)
        target = element.get("target", "").lstrip("#")
        if element.get("type") == "bibr":
            targets = [part.lstrip("#") for part in element.get("target", "").split()]
            linked = []
            for ref_id in targets:
                if ref_id in state.references:
                    state.cited_refs.add(ref_id)
                    marker = (visible or state.references[ref_id]["label"]).strip("[]")
                    linked.append(f"[{marker}]{{#{ref_id}}}")
            if linked:
                return " ".join(linked)
            return "[" + visible.strip("[]") + "]" if visible else ""
        if element.get("type") == "foot" and target in state.footnotes:
            number = state.used_footnotes.setdefault(target, len(state.used_footnotes) + 1)
            return f"[^{number}]"
        raw_target = element.get("target", "")
        if raw_target and not raw_target.startswith("#"):
            if not state.legacy and _web_url("".join(raw_target.split())):
                raw_target = "".join(raw_target.split())
            return f"{visible} ({raw_target})" if visible and visible != raw_target else raw_target
        return visible

    if tag == "ptr":
        target = element.get("target", "")
        compact = "".join(target.split())
        return compact if not state.legacy and _web_url(compact) else target

    if tag == "figure":
        state.figures += 1
        heading = _first_text(element, "t:head")
        description = _first_text(element, "t:figDesc")
        label = ": ".join(value for value in (heading, description) if value)
        parts = [f"\n\n**{'Table' if element.get('type') == 'table' else 'Figure'}:** {label}\n\n" if label else ""]
        for child in element:
            child_tag = etree.QName(child.tag).localname if isinstance(child.tag, str) else ""
            if child_tag not in {"head", "figDesc"}:
                parts.append(_render_element(child, state, depth))
                parts.append(child.tail or "")
        return "".join(parts)

    if tag == "table":
        return _render_table(element, state)

    if tag == "list":
        lines = []
        for item in element.xpath("t:item", namespaces=NS):
            content = _clean_text(_render_children(item, state, depth))
            if content:
                lines.append(f"- {content}")
        return "\n\n" + "\n".join(lines) + "\n\n" if lines else ""

    if tag == "note":
        if element.get("place") == "foot" and element.get(f"{{{XML}}}id") in state.footnotes:
            return ""
        content = _clean_text(_render_children(element, state, depth))
        return f"\n\n> {content}\n\n" if content else ""

    if tag == "formula":
        content = _text(element)
        return f" {content} " if content else ""

    return _render_children(element, state, depth)


def _render_document(xml_path: str | Path, profile: RenderProfile, version: str) -> tuple[str, dict[str, Any], list[str]]:
    if version not in RENDERER_VERSIONS:
        raise ValueError(f"Unsupported Markdown renderer version: {version}")
    legacy = version == LEGACY_RENDERER_VERSION
    validate_tei(xml_path)
    tree = etree.parse(str(xml_path), parser=etree.XMLParser(resolve_entities=False, no_network=True))
    root = tree.getroot()
    state = _RenderState(profile, legacy, _parse_references(root, legacy=legacy), _parse_footnotes(root))
    chunks: list[str] = []

    title = _first_text(root, "//t:titleStmt/t:title[@type='main']")
    if title:
        chunks.append(f"# {title}\n\n")
    authors = []
    for person in root.xpath("//t:sourceDesc//t:author/t:persName", namespaces=NS):
        name = _text(person)
        if name and name not in authors:
            authors.append(name)
    if authors:
        chunks.append(f"**Authors:** {', '.join(authors)}\n\n")

    abstract = root.xpath("//t:abstract", namespaces=NS)
    if abstract:
        chunks.append("## Abstract\n\n")
        chunks.append(_render_children(abstract[0], state, 1))

    body = root.find(f"{{{TEI}}}text/{{{TEI}}}body")
    body_markdown = "" if body is None else _render_children(body, state, 0)
    if legacy and not _clean_text(body_markdown):
        raise ValueError("Canonical TEI body rendered to empty Markdown")
    if profile == "full_body":
        chunks.append("\n\n## Body\n\n")
    chunks.append(body_markdown)

    back_markdown = ""
    if profile == "full_body" or not legacy:
        back = root.find(f"{{{TEI}}}text/{{{TEI}}}back")
        if back is not None:
            back_markdown = "".join(_render_element(child, state, 0) for child in back)
            chunks.append(back_markdown)
        for footnote_id in state.footnotes:
            state.used_footnotes.setdefault(footnote_id, len(state.used_footnotes) + 1)
    if not _clean_text(body_markdown + back_markdown):
        raise ValueError("Canonical TEI body and back matter rendered to empty Markdown")

    if state.used_footnotes:
        chunks.append("\n\n---\n\n## Footnotes\n\n")
        for footnote_id, number in sorted(state.used_footnotes.items(), key=lambda item: item[1]):
            chunks.append(f"[^{number}]: {state.footnotes[footnote_id]}\n\n")

    reference_ids = state.cited_refs if legacy and profile == "pruned" else set(state.references)
    unparsed_tables = []
    if reference_ids:
        chunks.append("\n\n---\n\n## References\n\n")
        table_lists = set()
        for ref_id in state.references:
            if ref_id not in reference_ids:
                continue
            ref = state.references[ref_id]
            authors_text = ", ".join(ref["authors"]) or "Unknown"
            links = "".join(f" DOI: https://doi.org/{doi}" for doi in ref["dois"] if doi)
            links += "".join(f" URL: {url}" for url in ref["urls"] if url)
            if version == RENDERER_VERSION:
                if DATASET_INVENTORY_CAPTION.match(ref["title"] or ref["raw"]):
                    table_lists.add(ref["list"])
                if ref["list"] in table_lists:
                    unparsed_tables.append(f"{UNPARSED_TABLE_PREFIX}{{#{ref_id}}} {ref['raw']}{links}\n\n")
                    continue
            reference_text = ref["raw"] if profile == "full_body" or (not legacy and not ref["title"]) else f"{authors_text} ({ref['year']}). {ref['title']}."
            chunks.append(f"- **[{ref['label']}]** {{#{ref_id}}} {reference_text}{links}\n\n")
    if unparsed_tables:
        chunks.append(f"\n\n---\n\n{UNPARSED_TABLE_HEADING}\n\n")
        chunks.extend(unparsed_tables)
        state.warnings.append("GROBID placed appendix table fragments in the bibliography. Review the source table for dataset rows.")

    markdown = "".join(chunks).replace("\r\n", "\n")
    if not legacy:
        markdown = _repair_delimited_links(markdown)
    markdown = re.sub(r"\n{4,}", "\n\n\n", markdown).strip() + "\n"
    body_characters = len("".join(body.itertext())) if body is not None else 0
    metrics: dict[str, Any] = {
        "profile": profile,
        "source_sections": state.source_sections,
        "rendered_sections": state.rendered_sections,
        "source_section_count": len(state.source_sections),
        "rendered_section_count": len(state.rendered_sections),
        "figures": state.figures,
        "tables": state.tables,
        "body_characters": body_characters,
        "rendered_characters": len(markdown),
        "references": len(reference_ids) - len(unparsed_tables),
    }
    if version == RENDERER_VERSION:
        metrics["unparsed_appendix_table_fragments"] = len(unparsed_tables)
    if profile == "pruned":
        state.warnings.append("Pruned profile omits sections and is not canonical for evaluation")
    return markdown, metrics, state.warnings


def extract_markdown(
    xml_path: str | Path, profile: RenderProfile = DEFAULT_RENDER_PROFILE, *, version: str = RENDERER_VERSION,
) -> str:
    """Return Markdown while preserving the historical string-returning API."""
    return _render_document(xml_path, profile, version)[0]


def _metadata_path(output_path: Path) -> Path:
    return output_path.with_suffix(output_path.suffix + ".meta.json")


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".part", dir=path.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _render_directory(profile: RenderProfile, version: str) -> Path:
    return MARKDOWN_DIR if version == LEGACY_RENDERER_VERSION else MARKDOWN_DIR / version / profile


def render_to_markdown(
    xml_path: Path,
    output_path: Path | None = None,
    paper_id: str | None = None,
    overwrite: bool = False,
    profile: RenderProfile = DEFAULT_RENDER_PROFILE,
    *,
    version: str = RENDERER_VERSION,
) -> RenderResult:
    xml_path = Path(xml_path)
    paper_id = paper_id or xml_path.stem.replace(".tei", "")
    output_path = Path(output_path) if output_path else _render_directory(profile, version) / f"{paper_id}.md"
    source_hash = sha256_file(xml_path) if xml_path.exists() else None
    metadata_path = _metadata_path(output_path)

    if output_path.exists() and not overwrite and source_hash:
        metadata = _read_metadata(metadata_path)
        if (
            metadata.get("producer_version") == version
            and metadata.get("source_sha256") == source_hash
            and metadata.get("profile") == profile
            and metadata.get("sha256") == sha256_file(output_path)
            and output_path.stat().st_size > 0
        ):
            metrics = metadata.get("quality_metrics") or {}
            return RenderResult(
                paper_id=paper_id,
                xml_path=xml_path,
                md_path=output_path,
                success=True,
                message=f"Reused validated render: {output_path.name}",
                sections_extracted=int(metrics.get("rendered_section_count", 0)),
                references_count=int(metrics.get("references", 0)),
                sha256=metadata["sha256"],
                source_sha256=source_hash,
                producer_version=version,
                profile=profile,
                warnings=list(metadata.get("warnings") or []),
                quality_metrics={**metrics, "cache_reused": True},
            )

    try:
        markdown, metrics, warnings = _render_document(xml_path, profile, version)
        _write_atomic(output_path, markdown.encode("utf-8"))
        digest = sha256_file(output_path)
        metadata = {
            "sha256": digest,
            "source_sha256": source_hash,
            "producer_version": version,
            "profile": profile,
            "warnings": warnings,
            "quality_metrics": metrics,
        }
        _write_atomic(metadata_path, json.dumps(metadata, indent=2, sort_keys=True).encode("utf-8"))
        return RenderResult(
            paper_id=paper_id,
            xml_path=xml_path,
            md_path=output_path,
            success=True,
            message=f"Rendered: {output_path.name}",
            sections_extracted=int(metrics["rendered_section_count"]),
            references_count=int(metrics["references"]),
            sha256=digest,
            source_sha256=source_hash,
            producer_version=version,
            profile=profile,
            warnings=warnings,
            quality_metrics={**metrics, "cache_reused": False},
        )
    except Exception as exc:
        return RenderResult(
            paper_id=paper_id,
            xml_path=xml_path,
            md_path=output_path,
            success=False,
            message=f"Render failed: {exc}",
            error=str(exc),
            source_sha256=source_hash,
            producer_version=version,
            profile=profile,
            warnings=[],
            quality_metrics={},
        )


def render_papers(
    papers: list[dict[str, Any]],
    output_dir: str | Path | None = None,
    paper_id_key: str = "paperId",
    xml_path_key: str = "xml_path",
    overwrite: bool = False,
    profile: RenderProfile = DEFAULT_RENDER_PROFILE,
    *,
    version: str = RENDERER_VERSION,
) -> list[RenderResult]:
    destination = Path(output_dir) if output_dir else _render_directory(profile, version)
    destination.mkdir(parents=True, exist_ok=True)
    results: list[RenderResult] = []
    for paper in papers:
        paper_id = str(paper.get(paper_id_key) or "unknown")
        xml_path = paper.get(xml_path_key)
        if not xml_path:
            results.append(
                RenderResult(
                    paper_id=paper_id,
                    xml_path=Path(),
                    md_path=destination / f"{paper_id}.md",
                    success=False,
                    message="Missing XML path",
                    producer_version=version,
                    error="Missing XML path",
                    profile=profile,
                )
            )
            continue
        results.append(
            render_to_markdown(
                Path(xml_path),
                output_path=destination / f"{paper_id}.md",
                paper_id=paper_id,
                overwrite=overwrite,
                profile=profile,
                version=version,
            )
        )
    logger.info("Rendered %s/%s documents", sum(result.success for result in results), len(results))
    return results
