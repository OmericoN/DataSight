"""Bind matching fields to explicit collection links and deposit citations."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_plus, urlsplit

from datasight.application.catalog_candidate_resolution import add_evidence
from datasight.application.catalog_features import REPOSITORY_NAME
from datasight.application.extraction_engine import Block
from datasight.infrastructure.ingestion.renderer import UNPARSED_TABLE_PREFIX
from datasight.matching.normalization import normalize_identifier

URL = re.compile(r"https?://[^\s<>\[\](),;]+", re.I)
AVAILABILITY = re.compile(r"\b(?:available|deposited|downloaded|accessed|obtained|retrieved)\b", re.I)
TCIA = re.compile(r"\b(?:The Cancer Imaging Archive(?: \(TCIA\))?|TCIA)\b", re.I)
EXPLICIT_USE = re.compile(r"\b(?:data(?:sets| sets)?|cohorts) (?:used|analy[sz]ed) in (?:this|our) (?:work|study|paper)\b", re.I)
DATASET_MARKER = re.compile(r"\[data\s?set\]", re.I)
TITLE_PREFIX = re.compile(r"^data (?:from|for)\b[: ]*", re.I)
REPLICATION_TITLE = re.compile(r"^replication data for\b", re.I)
GENERIC_ALIAS = re.compile(
    r"^(?:\d+|one|two|three|four|five|both|several|multiple|these|those)\b.*"
    r"\b(?:datasets?|data sets?|cohorts?|samples?|collections?)\b", re.I,
)
COMPACT_REFERENCE = re.compile(
    r"^- \*\*\[[^\]]+\]\*\* \{#[^}]+\} (?P<byline>[^()]*) "
    r"\((?P<year>\d{4}|)\)\. (?P<title>.+?)(?=\.\s|\.$|$)",
)
ZENODO_RECORD = re.compile(r"\. URL: (https?://zenodo\.org/records?/[0-9]+/?)\.?", re.I)
NUMBERED_TCIA_LINK = re.compile(
    r"\bdataset\s+\d+\s*[-:]\s*(?P<url>https?://wiki\.[ \t]*cancerimagingarchive\.net/display/Public/"
    r"(?P<slug>[A-Z0-9]+(?:[+_-][ \t]*[A-Z0-9]+)*))(?=[#;,\])]|[.](?:\s|$)|$)", re.I,
)
DATA_TITLE = re.compile(r"\b(?:Data From|Data for) (?P<name>.+?)(?=\s+(?:19|20)\d{2}(?:[. ]|$)|\.\s+(?:DOI|URL):|$)", re.I)
CITATION_DOI = re.compile(r"\bDOI:\s+(?P<doi>(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/[^\s]+)", re.I)
REFERENCE_ID = re.compile(r"\{#([^}]+)\}")
ARTICLE_LINK_LABEL = re.compile(
    r"\b(?:(?:article|paper|publication|manuscript)(?:['’]s)?\s+(?:doi|url|link)"
    r"|(?:doi|url|link)\s+(?:of|for|to)\s+(?:(?:this|the|our)\s+)?(?:article|paper|publication|manuscript))"
    r"\s*(?:is\b\s*)?[:=]?\s*(?:https?://(?:dx\.)?doi\.org/)?$", re.I,
)
SENTENCE_BREAK = re.compile(r"[.!?]\s+(?=[A-Z])")
SHORT_NAME = re.compile(r"\b(?:refer to as|referred to as|also known as|hereafter called)\s+(?:the\s+)?", re.I)
DESCRIPTOR_SUFFIX = re.compile(r"\s+(?:dataset|data set|cohort)$", re.I)
MATCHING_FIELDS = {"aliases", "identifiers", "urls", "creators", "year", "repository", "accessions"}


def _article_link(quote: str, literal: str) -> bool:
    if not literal:
        return False
    starts = [match.start() for match in re.finditer(re.escape(literal), quote)]
    return bool(starts) and all(ARTICLE_LINK_LABEL.search(quote, 0, start) for start in starts)


def collection_key(label: str) -> str:
    label = TITLE_PREFIX.sub("", DATASET_MARKER.sub("", label)).casefold()
    return " ".join(re.findall(r"[^\W_]+", label))


@dataclass(frozen=True)
class CollectionLink:
    url: str
    slug: str
    repository: str | None
    line: Block


@dataclass(frozen=True)
class Deposit:
    reference_id: str
    title: str
    creators: tuple[str, ...]
    year: int | None
    dois: tuple[str, ...]
    line: Block


def _tcia_slug(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    domain = parsed.netloc.casefold()
    path = parsed.path.rstrip("/")
    if (
        (domain == "wiki.cancerimagingarchive.net" and path.startswith("/display/Public/"))
        or (domain in {"cancerimagingarchive.net", "www.cancerimagingarchive.net"} and path.startswith("/collection/"))
    ):
        slug = path.rsplit("/", 1)[-1]
        return slug if slug and slug.casefold() not in {"public", "collection"} else None
    return None


def _deposit(quote: str, line: Block) -> Deposit | None:
    reference_id = REFERENCE_ID.search(quote)
    if not reference_id:
        return None
    reference = COMPACT_REFERENCE.match(quote)
    if reference:
        title = reference["title"]
        if not (
            TITLE_PREFIX.match(title)
            or REPLICATION_TITLE.match(title)
            or DATASET_MARKER.search(quote)
            or REPOSITORY_NAME.fullmatch(quote[reference.end():].partition("DOI:")[0].partition("URL:")[0].strip(". "))
        ):
            return None
        creators = tuple(person for person in reference["byline"].split(", ") if person and person != "Unknown")
        year = int(reference["year"]) if reference["year"] else None
    else:
        # Full-body bibliography preserves raw TEI field order.
        title_match = DATA_TITLE.search(quote)
        if not title_match or not CITATION_DOI.search(quote):
            return None
        title, creators, year = title_match[0], (), None
    dois: dict[str, str] = {}
    for match in CITATION_DOI.finditer(quote):
        literal = match["doi"].rstrip(".,;")
        if not ARTICLE_LINK_LABEL.search(quote, 0, match.start("doi")):
            dois.setdefault(normalize_identifier(literal), literal)
    return Deposit(reference_id[1], title, creators, year, tuple(dois.values()), line)


def has_explicit_deposit_citation(line: str) -> bool:
    return _deposit(line.rstrip(), Block(0, len(line))) is not None


def has_catalog_deposit_link(line: str) -> bool:
    line = line.rstrip()
    if not line.startswith("- **["):
        return False
    reference = COMPACT_REFERENCE.match(line)
    return bool(
        (reference and ZENODO_RECORD.fullmatch(line, reference.end()))
        or (("DOI:" in line or "URL:" in line) and has_explicit_deposit_citation(line))
    )


def _collection_links(line: str, known: set[str], block: Block) -> list[tuple[str, CollectionLink]]:
    if not AVAILABILITY.search(line):
        return []
    repo = TCIA.search(line)
    numbered = []

    def recover_numbered(match: re.Match[str]) -> str:
        url = "".join(match["url"].split())
        slug = "".join(match["slug"].split())
        key = collection_key(unquote_plus(slug))
        if url == match["url"] or key not in known:
            return match[0]
        numbered.append((key, CollectionLink(url, slug, repo[0] if repo else None, block)))
        return ""  # Exclude the repaired link from the parallel-list heuristic.

    line = NUMBERED_TCIA_LINK.sub(recover_numbered, line)
    raw_urls = list(URL.finditer(line))
    links = [
        (url, slug) for match in raw_urls
        if (slug := _tcia_slug(url := match[0].rstrip(".:"))) is not None
    ]
    if not links:
        return numbered
    prose = " " + collection_key(URL.sub("", line)) + " "
    names = {name for name in known if f" {name} " in prose}
    matching_names = []
    for _, slug in links:
        target = collection_key(unquote_plus(slug))
        exact = names & {target}
        partial = {name for name in names if f" {name} " in f" {target} "}
        matches = exact or partial
        matching_names.append(next(iter(matches)) if len(matches) == 1 else None)

    if not numbered and len(links) == len(raw_urls) == 1 and len(names) == 1:
        slug_key = collection_key(unquote_plus(links[0][1]))
        if slug_key not in known or slug_key in names:
            matching_names = [next(iter(names))]
        else:
            matching_names = [None]
    elif not numbered and len(links) == len(raw_urls) and any(matching_names):
        # Accept a parallel list only when its order agrees with a collection name
        # visible in the URL and every named member appears exactly once.
        for parenthesis in re.findall(r"\(([^()]+)\)", line[:raw_urls[0].start()]):
            ordered = [collection_key(item) for item in re.split(r",\s*(?:and\s+)?|\s+and\s+", parenthesis, flags=re.I)]
            if (len(ordered) == len(links) == len(names) and set(ordered) == names
                    and all(found is None or found == proposed for found, proposed in zip(matching_names, ordered))):
                matching_names = ordered
                break

    return numbered + [
        (name, CollectionLink(url, slug, repo[0] if repo else None, block))
        for name, (url, slug) in zip(matching_names, links) if name
    ]


def _named_citations(line: str, known: set[str]) -> list[tuple[str, str]]:
    if not REFERENCE_ID.search(line):
        return []
    citations = []
    for sentence in SENTENCE_BREAK.split(line):
        markers = REFERENCE_ID.findall(sentence)
        if not markers:
            continue
        prose = f" {collection_key(REFERENCE_ID.sub('', sentence))} "
        names = [name for name in known if f" {name} " in prose]
        if len(names) == 1:
            citations.extend((reference_id, names[0]) for reference_id in markers)
    return citations


def _add_fact(identity: dict[str, Any], field: str, value: Any, evidence_id: str) -> None:
    observation = identity.setdefault(field, {"state": "reported", "values": []})
    for item in observation["values"]:
        if item["value"] == value:
            if evidence_id not in item["evidence_ids"]:
                item["evidence_ids"].append(evidence_id)
            return
    observation["values"].append({"value": value, "evidence_ids": [evidence_id]})


def _link_declared_short_names(
    groups: list[dict[str, Any]], by_id: dict[str, dict[str, Any]], links: dict[str, list[CollectionLink]],
) -> None:
    named: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in groups:
        if group["name"] and not group["identity_uncertain"]:
            named[collection_key(group["name"])].append(group)
    associations = []
    for name, entries in named.items():
        for group in entries:
            for item in group["identity"].get("aliases", {}).get("values", []):
                label = item["value"]
                shorter = DESCRIPTOR_SUFFIX.sub("", label)
                short_key = collection_key(shorter)
                if short_key == name or short_key not in named:
                    continue
                current_links = {collection_key(unquote_plus(link.slug)) for link in links.get(name, [])}
                short_links = {collection_key(unquote_plus(link.slug)) for link in links.get(short_key, [])}
                if current_links and short_links and current_links.isdisjoint(short_links):
                    continue
                for eid in item["evidence_ids"]:
                    quote = by_id[eid]["quote"]
                    declaration = next((m for m in SHORT_NAME.finditer(quote) if quote[m.end():].startswith(label)), None)
                    if declaration and group["name"] in SENTENCE_BREAK.split(quote[:declaration.start()])[-1]:
                        associations.append((group, shorter, short_key, eid))
    for group, shorter, short_key, eid in associations:
        _add_fact(group["identity"], "aliases", shorter, eid)
        for entry in named[short_key]:
            _add_fact(entry["identity"], "aliases", group["name"], eid)


def reconcile_catalog_identity(text: str, groups: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> None:
    if not groups:
        return
    names = {
        key for group in groups if not group["identity_uncertain"]
        and (key := collection_key(group["name"] or "")) and any(char.isalpha() for char in key)
    }
    links: dict[str, list[CollectionLink]] = defaultdict(list)
    deposits: list[Deposit] = []
    # Only a standalone record URL after the complete citation title qualifies.
    # Keep distinct records with identical titles ambiguous.
    zenodo_records: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    cited_by_name: dict[str, dict[str, list[Block]]] = defaultdict(lambda: defaultdict(list))
    start = 0
    for raw in text.splitlines(keepends=True):
        line = raw.rstrip("\r\n")
        span = Block(start, start + len(line))
        start += len(raw)
        if line.startswith(UNPARSED_TABLE_PREFIX + "{#"):
            continue
        if line.startswith("- **["):
            reference = COMPACT_REFERENCE.match(line)
            if reference and (record := ZENODO_RECORD.fullmatch(line, reference.end())):
                zenodo_records[reference["title"].casefold()][record[1]].add(span.start)
            if citation := _deposit(line, span):
                deposits.append(citation)
        else:
            for name, collection in _collection_links(line, names, span):
                links[name].append(collection)
            for reference_id, name in _named_citations(line, names):
                cited_by_name[reference_id][name].append(span)

    by_id = {item["evidence_id"]: item for item in evidence}
    collection_owners: dict[str, set[str]] = defaultdict(set)
    for name, entries in links.items():
        collection_owners[name].add(name)
        for entry in entries:
            collection_owners[collection_key(unquote_plus(entry.slug))].add(name)
    body_names = {
        collection_key(group["name"]) for group in groups
        if group["name"] and not group["identity_uncertain"] and not group["bibliography_only"]
    }
    linked_titles = set(collection_owners)
    for deposit in deposits:
        cited = cited_by_name.get(deposit.reference_id, {})
        title = collection_key(deposit.title)
        if len(cited) == 1 and title not in linked_titles and title not in body_names:
            collection_owners[title].update(cited)

    deposits_by_name: dict[str, list[Deposit]] = defaultdict(list)
    for deposit in deposits:
        title = collection_key(deposit.title)
        owners = collection_owners.get(title, set())
        if len(owners) > 1:
            continue
        deposits_by_name[title].append(deposit)
        for owner in owners - {title}:
            if title in linked_titles or set(cited_by_name.get(deposit.reference_id, {})) == {owner}:
                deposits_by_name[owner].append(deposit)

    for group in groups:
        name = collection_key(group["name"] or "")
        group_links = links.get(name, []) if not group["identity_uncertain"] else []
        relevant_deposits = deposits_by_name.get(name, []) if name and not group["identity_uncertain"] else []
        records = zenodo_records.get((group["name"] or "").casefold(), {}) if not group["identity_uncertain"] else {}
        owned_citations = {deposit.line.start for deposit in relevant_deposits}
        ambiguous_dois = {deposit.line.start for deposit in relevant_deposits if len(deposit.dois) > 1}
        identity = group.get("identity") or {}
        group["identity"] = identity
        rejected = set(group.get("review_reasons", []))
        if ambiguous_dois:
            rejected.add("Review dataset identifiers evidence")
        for field in MATCHING_FIELDS & identity.keys():
            observation = identity[field]
            retained = []
            for value in observation["values"]:
                literal = value["value"]
                collection = (
                    collection_key(unquote_plus(_tcia_slug(literal["url"]) or "")) if field == "urls"
                    else collection_key(literal) if field == "aliases" else ""
                )
                owners = collection_owners.get(collection, set())
                safe = not owners or name in owners or name == collection
                if field == "aliases" and GENERIC_ALIAS.match(literal):
                    safe = False
                is_doi = ((field == "identifiers" and literal["scheme"] == "doi")
                          or (field == "urls" and normalize_identifier(literal["url"]).startswith("10.")))
                link_literal = literal["literal"] if field == "identifiers" else literal["url"] if field == "urls" else None
                record_lines = records.get(literal["url"], set()) if field == "urls" and len(records) == 1 else set()
                valid_ids = [
                    eid for eid in value["evidence_ids"]
                    if safe and (by_id[eid]["placement"] != "bibliography" or by_id[eid]["char_start"] in owned_citations
                                 or by_id[eid]["char_start"] in record_lines)
                    and not (is_doi and by_id[eid]["char_start"] in ambiguous_dois)
                    and not (link_literal and _article_link(by_id[eid]["quote"], link_literal))
                ]
                if valid_ids:
                    if record_lines:
                        value = {**value, "value": {**literal, "target": "landing_page"}}
                    retained.append({**value, "evidence_ids": valid_ids})
                if len(valid_ids) != len(value["evidence_ids"]):
                    rejected.add(f"Review dataset {field} evidence")
            if retained:
                observation["values"] = retained
                if observation["state"] == "conflicting" and len(retained) == 1:
                    observation["state"] = "reported"
            else:
                del identity[field]

        for link in group_links:
            eid = add_evidence(text, link.line, by_id)
            _add_fact(identity, "urls", {"url": link.url, "target": "landing_page"}, eid)
            if link.repository:
                _add_fact(identity, "repository", link.repository, eid)
            if collection_key(link.slug) != name:
                _add_fact(identity, "aliases", link.slug, eid)
            if EXPLICIT_USE.search(by_id[eid]["quote"]):
                role_ids = group.setdefault("roles", {}).setdefault("used", [])
                if eid not in role_ids:
                    role_ids.append(eid)
                if eid not in group["evidence_ids"]:
                    group["evidence_ids"].append(eid)
        for deposit in relevant_deposits:
            eid = add_evidence(text, deposit.line, by_id)
            if collection_key(deposit.title) != name:
                _add_fact(identity, "aliases", deposit.title, eid)
            if len(deposit.dois) == 1:
                value = {"scheme": "doi", "literal": deposit.dois[0], "target": "dataset"}
                _add_fact(identity, "identifiers", value, eid)
                for citation in cited_by_name.get(deposit.reference_id, {}).get(name, []):
                    _add_fact(identity, "identifiers", value, add_evidence(text, citation, by_id))
            for creator in deposit.creators:
                _add_fact(identity, "creators", creator, eid)
            if deposit.year:
                _add_fact(identity, "year", deposit.year, eid)
        for field in ("year", "identifiers"):
            observation = identity.get(field)
            if not observation:
                continue
            if field == "year":
                conflicting = len(observation["values"]) > 1
            else:
                dois = {
                    normalize_identifier(item["value"]["literal"]) for item in observation["values"]
                    if item["value"]["scheme"] == "doi"
                }
                conflicting = len(dois) > 1
            if conflicting:
                observation["state"] = "conflicting"
                rejected.add(f"Review dataset {field} evidence")
        group["review_reasons"] = sorted(rejected)
    _link_declared_short_names(groups, by_id, links)
    evidence[:] = sorted(by_id.values(), key=lambda item: item["char_start"])
