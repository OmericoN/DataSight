"""Sparse, source-grounded catalog features for dataset extraction."""

import re
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import StrictBool, StrictInt

from datasight.application.extraction_engine import INSTRUCTIONS
from datasight.application.catalog_candidate_resolution import ACCESSION
from datasight.application.whole_document_extraction import CandidateGroup, WholeDocumentResponse
from datasight.domain.candidate_detection import COMPILED_TRIGGERS
from datasight.domain.extraction import Evidence, IdentityFeatures, StrictModel
from datasight.infrastructure.llm.client import InvalidCompletion

CATALOG_PROMPT = INSTRUCTIONS + """Read all numbered lines [id,text]. Return c=true if the scan is complete
and g with one group per distinct dataset, including unnamed data and explicit dataset citations.
Each group is [name or null, identity uncertain, reference/support line IDs, facts].
Copy the dataset name exactly. Capture alternate names in parentheses, acronym expansions and
repository collection titles as aliases, even in a line listing multiple cohorts. Never repeat the name as an alias.
Keep ambiguous identities and different versions separate. An article or repository alone is not a dataset.
Each fact is [field, line ID, exact substring]. Include its line in the group's evidence.
Extract only facts that belong to this dataset. Include each available fact once:
creators (one person or organization per fact), dataset publication/release year, dataset DOI,
Handle, repository, accession, landing or download URL, version, subset or wave, access date.
For description copy one short distinctive phrase about the data (e.g. population, location or modality).
Prefer specific collection names, aliases, accessions, repository names and links over long descriptions,
sample counts and analysis details. Include explicit data deposits listed in the references with their own
title, deposit creators, year and identifier. Skip article metadata. Omit absent or uncertain facts.
A paper DOI, paper author, study year, repository homepage or software URL cannot identify the dataset.
For a DOI resolver URL, emit the complete URL as doi. Skip duplicate URL facts for that resolver.
Include all distinct datasets sharing a line. When a line defines several acronym/repository
pairs, attach each alias and repository to its own dataset. Minify JSON on one line without indentation.
For "OS (Oriole Survey) from Dryad" the group is ["OS",false,[4],[["aliases",4,"Oriole Survey"],["repository",4,"Dryad"]]].
An empty g is valid."""

CatalogField = Literal[
    "aliases", "description", "creators", "year", "doi", "handle", "repository", "accessions",
    "landing_page", "download", "versions", "subsets", "access_dates",
]
DEPOSIT_MARKERS = (
    re.compile(r"\[data\s?set\]|\bdata from:|\b(?:replication data for|data repository|data archive|imaging archive)\b", re.I),
    *(pattern for kind, pattern, _ in COMPILED_TRIGGERS if kind in {"repository_name", "repository_url"}),
)
REPOSITORY_NAME = next(pattern for kind, pattern, _ in COMPILED_TRIGGERS if kind == "repository_name")


def has_deposit_marker(line: str) -> bool:
    return any(pattern.search(line) for pattern in DEPOSIT_MARKERS)


RoleField = Literal["role_used", "role_created", "role_comparison", "role_discussed"]
ROLE_PROMPT = CATALOG_PROMPT + """
Also report the current paper's explicitly evidenced role for each dataset as role_used,
role_created, role_comparison or role_discussed. Quote the shortest exact clause that
ties the action to this dataset. Omit roles for ambiguous attribution, prior work,
and bibliography-only evidence. A data deposit alone does not establish a role."""


class CatalogChunkResponse(StrictModel):
    c: StrictBool
    g: list[tuple[str | None, StrictBool, list[StrictInt], list[tuple[CatalogField, StrictInt, str]]]]

    def expand(self) -> WholeDocumentResponse:
        return WholeDocumentResponse(complete=self.c, groups=[
            CandidateGroup(name=name, identity_uncertain=uncertain, line_ids=ids)
            for name, uncertain, ids, _ in self.g
        ])


class CatalogUseChunkResponse(CatalogChunkResponse):
    g: list[tuple[str | None, StrictBool, list[StrictInt], list[tuple[CatalogField | RoleField, StrictInt, str]]]]


def catalog_value(field: CatalogField, literal: str, source: Evidence) -> tuple[str, Any] | None:
    quote = source.quote
    if not literal.strip() or literal not in quote:
        return None
    if (source.placement == "bibliography" and field in {"creators", "year", "doi", "handle", "landing_page", "download"}
            and not has_deposit_marker(quote)):
        return None
    if field == "aliases" and re.search(
        rf"(?:version of|subset of|derived from)\s+(?:the\s+)?{re.escape(literal)}\b", quote, re.I,
    ):
        return None
    if field == "aliases" and (REPOSITORY_NAME.fullmatch(literal) or literal.casefold() == "the cancer imaging archive"):
        return "repository", literal
    if field in {"year", "doi", "handle", "accessions", "landing_page", "download"} and not re.search(
        rf"(?<!\w){re.escape(literal)}(?![\w/#?=&%-]|\.[\w])", quote,
    ):
        return None
    if field == "year":
        return ("year", int(literal)) if re.fullmatch(r"[0-9]{4}", literal) else None
    if field in {"doi", "handle"}:
        pattern = (r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?10\.\d{4,9}/\S+" if field == "doi"
                   else r"(?:https?://hdl\.handle\.net/|hdl:\s*)?\d[\d.]*/\S+")
        if not re.fullmatch(pattern, literal, re.I):
            return None
        return "identifiers", {"scheme": field, "literal": literal, "target": "dataset"}
    if field in {"landing_page", "download"}:
        if not re.fullmatch(r"(?:https?://|www\.)[^\s/]+(?:/\S*)?", literal, re.I):
            return None
        return "urls", {"url": literal, "target": field}
    return field, literal


def citation_facts(field: CatalogField, literal: str, source: Evidence) -> Iterator[tuple[CatalogField, str]]:
    # The pruned renderer writes the cited surnames and year together after the reference ID.
    if field == "creators" and source.placement == "bibliography" and f"}} {literal}." in source.quote:
        byline = re.fullmatch(r"([^()]+) \(([0-9]{4})\)", literal)
        if byline:
            for author in byline[1].split(", "):
                if author != "Unknown":
                    yield "creators", author
            yield "year", byline[2]
            return
    yield field, literal


def attach_catalog_facts(response: CatalogChunkResponse, groups: list[dict[str, Any]],
                         evidence: dict[str, Evidence], *, recover_fields: bool = False) -> None:
    for (name, _, ids, facts), group in zip(response.g, groups, strict=True):
        by_line = dict(zip(dict.fromkeys(ids), group["evidence_ids"], strict=True))
        if name and group["name"] and ACCESSION.fullmatch(name):
            line = next(line for line, eid in by_line.items() if name in evidence[eid].quote)
            facts = [*facts, ("accessions", line, name)]
        observations: dict[str, dict[str, Any]] = {}
        roles: dict[str, list[str]] = {}
        rejected = set()
        grounded = []
        for field, line, literal in facts:
            if line not in by_line:
                if recover_fields:
                    rejected.add(field)
                    continue
                raise InvalidCompletion("Invalid completion: catalog fact cites a line outside its dataset")
            eid = by_line[line]
            grounded.extend((key, value, eid) for key, value in citation_facts(field, literal, evidence[eid]))
        for field, literal, eid in grounded:
            if field == "aliases" and group["name"] == literal:
                continue
            if field.startswith("role_"):
                source = evidence[eid]
                if (source.placement == "bibliography" or not literal.strip() or literal not in source.quote
                        or (recover_fields and field != "role_discussed" and prior_work_clause(source.quote, literal))):
                    rejected.add(field)
                else:
                    ids_for_role = roles.setdefault(field.removeprefix("role_"), [])
                    if eid not in ids_for_role:
                        ids_for_role.append(eid)
                continue
            fact = catalog_value(field, literal, evidence[eid])
            if fact is None:
                rejected.add(field)
                continue
            key, value = fact
            values = observations.setdefault(key, {"state": "reported", "values": []})["values"]
            existing = next((item for item in values if item["value"] == value), None)
            if existing is None:
                values.append({"value": value, "evidence_ids": [eid]})
            elif eid not in existing["evidence_ids"]:
                existing["evidence_ids"].append(eid)
        if len(observations.get("year", {}).get("values", [])) > 1:
            observations["year"]["state"] = "conflicting"
            rejected.add("year")
        group["roles"] = roles
        group["identity"] = IdentityFeatures.model_validate(observations).model_dump(mode="json", exclude_unset=True)
        group["review_reasons"] = [f"Review dataset {field} evidence" for field in sorted(rejected)]


PRIOR_WORK = re.compile(
    r"\b(?:a|another|one|the previous|a prior|an earlier|a previous|a cited) study\b|"
    r"\b(?:prior|previous|earlier) (?:studies|work)\b|\bet al\.\s+(?:used|analy[sz]ed|reported|found)\b", re.I,
)
CURRENT_USE = re.compile(
    r"\b(?:we|this (?:study|paper|work)|our (?:study|analysis))\b"
    r"[^.;:]*?\b(?:used|analy[sz]ed|re-analy[sz]ed|collected|evaluated|trained|tested|validated|applied)\b", re.I,
)


def prior_work_clause(quote: str, literal: str) -> bool:
    """Catch explicit attribution to another study in the cited sentence."""
    for match in re.finditer(re.escape(literal), quote):
        preceding = re.split(r"[.!?]\s+(?=[A-Z])", quote[:match.start()])[-1]
        following = re.split(r"[.!?]\s+(?=[A-Z])", quote[match.end():], maxsplit=1)[0]
        sentence = preceding + literal + following
        prior = PRIOR_WORK.search(sentence)
        current = CURRENT_USE.search(sentence)
        if prior and (not current or (prior.start() < current.start() and current.end() > len(preceding + literal))):
            return True
    return False
