"""Layered UM dataset matching without paid vector services."""

from __future__ import annotations

from collections.abc import Iterable

from datasight.domain.schemas import DatasetMention, MatchStatus, UMDatasetRecord, UMMatchDecision
from datasight.matching.normalization import jaccard, normalize_identifier, normalize_text


def match_mention_to_um_dataset(
    mention: DatasetMention,
    candidates: Iterable[UMDatasetRecord],
    possible_threshold: float = 0.72,
    ambiguity_tolerance: float = 0.02,
) -> UMMatchDecision:
    """Match one extracted mention against UM metadata without arbitrary tie-breaking."""
    candidate_list = list(candidates)
    pids = {normalize_identifier(value) for value in [
        mention.metadata.persistent_identifier, *mention.metadata.dataset_identifiers,
    ]} - {""}
    urls = {normalize_identifier(value) for value in [
        str(mention.metadata.dataset_url or ""), *mention.metadata.dataset_urls,
    ]} - {""}

    exact_records = []
    for record in candidate_list:
        record_doi = normalize_identifier(record.doi)
        record_url = normalize_identifier(record.url)
        if record_doi in pids:
            exact_records.append((record, "exact_pid", "doi"))
        elif record_url in urls:
            exact_records.append((record, "exact_url", "url"))
    if len(exact_records) > 1:
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

    mention_names = {normalize_text(name) for name in [mention.dataset_name, *mention.aliases]} - {""}
    exact_name_records = []
    for record in candidate_list:
        title = normalize_text(record.title)
        # The UM export includes deposited collections titled "Data From <collection name>".
        collection = title.removeprefix("data from ").removeprefix("data from: ")
        if mention_names.intersection({title, collection, *(normalize_text(alias) for alias in record.aliases)}):
            exact_name_records.append(record)
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
    for record in candidate_list:
        fields: list[str] = []
        score = 0.0
        title_score = max((jaccard(name, title) for name in mention_names
                           for title in [record.title, *record.aliases]), default=0.0)
        if title_score:
            fields.append("title_or_alias")
            score += title_score * 0.6

        record_creators = {normalize_text(creator) for creator in record.creators}
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
