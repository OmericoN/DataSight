"""Layered UM dataset matching without paid vector services."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from functools import cached_property

from datasight.domain.schemas import DatasetMention, DatasetMetadata, MatchStatus, UMDatasetRecord, UMMatchDecision
from datasight.matching.normalization import catalog_link_key, normalize_text


def _conflicting_links(metadata: DatasetMetadata) -> list[str]:
    identity = (metadata.features or {}).get("identity")
    if not isinstance(identity, dict):
        return []
    literals = []
    for field, key, targets in (
        ("identifiers", "literal", {"dataset"}),
        ("urls", "url", {"landing_page", "download"}),
    ):
        observation = identity.get(field)
        if not isinstance(observation, dict) or observation.get("state") != "conflicting":
            continue
        for item in observation.get("values") or []:
            value = item.get("value") if isinstance(item, dict) else None
            if isinstance(value, dict) and value.get("target") in targets and isinstance(value.get(key), str):
                literals.append(value[key])
    return literals


def match_mention_to_um_dataset(
    mention: DatasetMention,
    candidates: Iterable[UMDatasetRecord],
    possible_threshold: float = 0.72,
    ambiguity_tolerance: float = 0.02,
) -> UMMatchDecision:
    return UMCatalogMatcher(candidates).match(mention, possible_threshold, ambiguity_tolerance)


class UMCatalogMatcher:
    """Reuse normalized UM metadata across a batch of mentions."""

    def __init__(self, records: Iterable[UMDatasetRecord]) -> None:
        self.records = {record.um_dataset_id: record for record in records}
        self.links: dict[str, set[str]] = defaultdict(set)
        for record in self.records.values():
            for raw in (record.doi, record.url):
                if key := catalog_link_key(raw):
                    self.links[key].add(record.um_dataset_id)

    @cached_property
    def names(self) -> dict[str, set[str]]:
        names: dict[str, set[str]] = defaultdict(set)
        for record in self.records.values():
            title = normalize_text(record.title)
            collection = re.sub(r"^(?:data from|data for|replication data for)\b\s*:?\s*", "", title)
            for name in (title, collection, *(normalize_text(alias) for alias in record.aliases)):
                if name:
                    names[name].add(record.um_dataset_id)
        return names

    @cached_property
    def similarity_fields(self) -> dict[str, tuple[list[set[str]], set[str]]]:
        titles: dict[str, list[set[str]]] = defaultdict(list)
        for name, ids in self.names.items():
            tokens = set(name.split())
            for record_id in ids:
                titles[record_id].append(tokens)
        return {
            key: (titles[key], {normalize_text(c) for c in record.creators} - {""})
            for key, record in self.records.items()
        }

    def match(
        self, mention: DatasetMention, possible_threshold: float = 0.72, ambiguity_tolerance: float = 0.02,
    ) -> UMMatchDecision:
        metadata = mention.metadata
        conflicting = _conflicting_links(metadata)
        literals = [metadata.persistent_identifier, *metadata.dataset_identifiers,
                    str(metadata.dataset_url or ""), *metadata.dataset_urls, *conflicting]
        keys = {catalog_link_key(value) for value in literals} - {""}
        exact_records = []
        for record_id in sorted({rid for key in keys for rid in self.links.get(key, ())}):
            record = self.records[record_id]
            if catalog_link_key(record.doi) in keys:
                exact_records.append((record, "exact_pid", "doi"))
            else:
                exact_records.append((record, "exact_url", "url"))
        if len(exact_records) > 1 or (conflicting and exact_records):
            return UMMatchDecision(
                publication_id=mention.publication_id, dataset_name=mention.dataset_name,
                status=MatchStatus.REVIEW_REQUIRED, match_method="ambiguous_exact_identifier",
                candidate_um_dataset_ids=sorted(record.um_dataset_id for record, _, _ in exact_records),
                matched_fields=sorted({field for _, _, field in exact_records}), match_score=1.0,
                review_required=True,
            )
        if exact_records:
            record, method, field = exact_records[0]
            return _decision(mention, record, MatchStatus.MATCHED, 1.0, method, [field])

        names = [mention.dataset_name, *mention.aliases]
        # Consolidated entries can have several source-backed names beyond the display name.
        identity = (metadata.features or {}).get("identity")
        if isinstance(identity, dict):
            observed = identity.get("name")
            if isinstance(observed, dict) and observed.get("state") == "reported":
                names.extend(item["value"] for item in observed.get("values") or []
                             if isinstance(item, dict) and isinstance(item.get("value"), str))
        mention_names = {normalize_text(name) for name in names} - {"", "unnamed dataset", "author-described dataset"}
        exact_name_ids = {key for name in mention_names for key in self.names.get(name, ())}
        exact_name_records = [self.records[key] for key in sorted(exact_name_ids)]
        if len(exact_name_records) > 1:
            return UMMatchDecision(
                publication_id=mention.publication_id,
                dataset_name=mention.dataset_name,
                status=MatchStatus.REVIEW_REQUIRED,
                candidate_um_dataset_ids=[record.um_dataset_id for record in exact_name_records],
                match_method="ambiguous_exact_title_or_alias",
                match_score=0.85,
                matched_fields=["title_or_alias"],
                review_required=True,
            )
        if exact_name_records:
            return _decision(
                mention,
                exact_name_records[0],
                MatchStatus.POSSIBLE,
                0.85,
                "exact_title_or_alias",
                ["title_or_alias"],
                review_required=True,
            )

        scored: list[tuple[float, UMDatasetRecord, list[str]]] = []
        mention_authors = {normalize_text(author) for author in mention.metadata.dataset_authors}
        mention_tokens = [set(name.split()) for name in mention_names]
        for record_id, (titles, record_creators) in self.similarity_fields.items():
            record = self.records[record_id]
            fields: list[str] = []
            score = 0.0
            title_score = max((len(name & title) / len(name | title)
                               for name in mention_tokens for title in titles), default=0.0)
            if title_score:
                fields.append("title_or_alias")
                score += title_score * 0.6

            if mention_authors and record_creators:
                author_overlap = len(mention_authors & record_creators) / len(
                    mention_authors | record_creators
                )
                if author_overlap:
                    fields.append("creators")
                    score += author_overlap * 0.25

            if mention.metadata.dataset_year and record.year:
                if mention.metadata.dataset_year == record.year:
                    fields.append("year")
                    score += 0.15
                elif abs(mention.metadata.dataset_year - record.year) == 1:
                    fields.append("near_year")
                    score += 0.05
            scored.append((min(score, 0.99), record, fields))

        scored.sort(key=lambda item: (-item[0], item[1].um_dataset_id))
        best_score, best_record, best_fields = scored[0] if scored else (0.0, None, [])
        if best_record and best_score >= possible_threshold:
            competing = [
                item for item in scored if item[0] >= possible_threshold and best_score - item[0] <= ambiguity_tolerance
            ]
            if len(competing) > 1:
                return UMMatchDecision(
                    publication_id=mention.publication_id,
                    dataset_name=mention.dataset_name,
                    status=MatchStatus.REVIEW_REQUIRED,
                    candidate_um_dataset_ids=[item[1].um_dataset_id for item in competing],
                    match_method="ambiguous_metadata_similarity",
                    match_score=best_score,
                    matched_fields=best_fields,
                    review_required=True,
                )
            return _decision(
                mention,
                best_record,
                MatchStatus.POSSIBLE,
                best_score,
                "metadata_similarity",
                best_fields,
                review_required=True,
            )

        return UMMatchDecision(
            publication_id=mention.publication_id,
            dataset_name=mention.dataset_name,
            status=MatchStatus.NO_MATCH,
            match_method="no_match",
            match_score=best_score,
            matched_fields=best_fields,
            review_required=False,
        )


def _decision(
    mention: DatasetMention,
    record: UMDatasetRecord,
    status: MatchStatus,
    score: float,
    method: str,
    fields: list[str],
    review_required: bool = False,
) -> UMMatchDecision:
    return UMMatchDecision(
        publication_id=mention.publication_id,
        dataset_name=mention.dataset_name,
        status=status,
        um_dataset_id=record.um_dataset_id,
        match_method=method,
        match_score=score,
        matched_fields=fields,
        review_required=review_required,
    )
