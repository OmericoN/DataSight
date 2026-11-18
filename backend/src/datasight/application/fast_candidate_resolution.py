"""Resolve source-local table labels and guard fast candidate consolidation."""

import re
from collections import defaultdict
from typing import Any

from datasight.application.extraction_engine import Block, evidence_for, specific_name
from datasight.domain.candidate_detection import COMPILED_TRIGGERS
from datasight.domain.extraction import LocatedPassage

LOCAL_LABEL = re.compile(r"D\d+", re.IGNORECASE)
VERSION = re.compile(r"\b(?:v(?:ersion)?\.?|release)\s*(\d[\w.-]*)", re.IGNORECASE)
ACCESSION = next(pattern for kind, pattern, _ in COMPILED_TRIGGERS if kind == "accession")
GENERIC = re.compile(r"^(?:\d|(?:datasets?|cohorts?|samples?|data|unnamed)\b)", re.IGNORECASE)


def table_cells(line: str) -> list[tuple[int, str]]:
    pipes = [match.start() for match in re.finditer(r"(?<!\\)\|", line)]
    cells = []
    for left, right in zip(pipes, pipes[1:]):
        raw = line[left + 1:right]
        cells.append((right - len(raw.lstrip()), raw.strip()))
    return cells


def table_labels(text: str) -> dict[str, dict[str, Block]]:
    labels: dict[str, dict[str, Block]] = defaultdict(dict)
    in_dataset_table = False
    start = 0
    for line in text.splitlines(keepends=True):
        row = line.removesuffix("\n").removesuffix("\r")
        cells = table_cells(row) if row.startswith("|") else []
        if len(cells) < 2:
            in_dataset_table = False
        else:
            key, name = cells[0][1], cells[1][1]
            if name.casefold() in {"dataset", "dataset name"}:
                in_dataset_table = key.casefold().rstrip(".") in {"abbre", "abbrev", "abbreviation", "id", "label", "ref"}
            elif in_dataset_table and name:
                label = key.split(maxsplit=1)[0] if key else ""
                if LOCAL_LABEL.fullmatch(label) and name != label:
                    labels[label.upper()].setdefault(name, Block(start, start + len(row)))
        start += len(line)
    return labels


def add_evidence(text: str, block: Block, by_id: dict[str, dict[str, Any]]) -> str:
    evidence_id = f"e{block.start}_{block.end}"
    if evidence_id not in by_id:
        passage = LocatedPassage(block_id=block.block_id, quote=text[block.start:block.end],
                                 quote_occurrence=0, dataset_name=None, kind="reference")
        by_id[evidence_id] = evidence_for(text, block, passage).model_dump(mode="json")
    return evidence_id


def refine_candidates(text: str, groups: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> set[str]:
    by_id = {item["evidence_id"]: item for item in evidence}
    labels = table_labels(text)
    blocked: set[str] = set()
    named_in_chunk: set[tuple[str, str]] = set()
    versions: dict[str, set[str]] = defaultdict(set)
    accessions: dict[str, set[str]] = defaultdict(set)
    names_by_evidence: dict[str, set[str]] = defaultdict(set)
    alias_owners: dict[str, set[str]] = defaultdict(set)

    for group in groups:
        name = group["name"]
        if name is None:
            continue
        original = name
        mappings = labels.get(name.upper(), {})
        if len(mappings) == 1:
            name, block = next(iter(mappings.items()))
            group["name"] = name
            row_id = add_evidence(text, block, by_id)
            group["evidence_ids"] = list(dict.fromkeys([*group["evidence_ids"], row_id]))

        key = specific_name(name)
        if key is None:
            continue
        if group["identity_uncertain"] or GENERIC.match(name) or LOCAL_LABEL.fullmatch(name):
            blocked.add(key)
        for alias in group.get("identity", {}).get("aliases", {}).get("values", []):
            if alias_key := specific_name(alias["value"]):
                alias_owners[alias_key].add(key)
        if original == name:
            chunk_name = (group["chunk"], key)
            if chunk_name in named_in_chunk:
                blocked.add(key)
            named_in_chunk.add(chunk_name)
        for evidence_id in group["evidence_ids"]:
            quote = by_id[evidence_id]["quote"]
            if name in quote:
                versions[key].update(value.rstrip(".").casefold() for value in VERSION.findall(quote))
                accessions[key].update(value.casefold() for value in ACCESSION.findall(quote))
                names_by_evidence[evidence_id].add(name)

    for group in groups:
        name = group["name"]
        if name is None:
            continue
        for index, evidence_id in enumerate(group["evidence_ids"]):
            item = by_id[evidence_id]
            names = names_by_evidence[evidence_id]
            if item["placement"] != "table" or len(names) < 2:
                continue
            matches = [(offset, cell) for offset, cell in table_cells(item["quote"]) if name in cell]
            if len(matches) != 1:
                continue
            offset, cell = matches[0]
            if any(other != name and other in cell for other in names):
                continue
            start = item["char_start"] + offset
            group["evidence_ids"][index] = add_evidence(text, Block(start, start + len(cell)), by_id)
        group["evidence_ids"] = list(dict.fromkeys(group["evidence_ids"]))

    evidence[:] = sorted(by_id.values(), key=lambda item: item["char_start"])
    blocked.update(name for qualifiers in (versions, accessions) for name, values in qualifiers.items() if len(values) > 1)
    blocked.update({alias for alias, owners in alias_owners.items() if len(owners) > 1 or owners & blocked})
    return blocked
