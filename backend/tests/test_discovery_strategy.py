from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.domain.discovery_strategy import (
    DiscoveryPhase,
    EvidenceReason,
    build_direct_queries,
    build_exact_queries,
    build_focused_queries,
    build_citing_queries,
    build_random_query,
    distinctive_titles,
    informative_keywords,
    profile_um_catalog,
    rank_strategy_candidates,
    structured_direct_dataset_ids,
    verified_exact_dataset_ids,
)
from datasight.domain.schemas import UMDatasetRecord


def record(index: int, **kwargs) -> UMDatasetRecord:
    values = {
        "um_dataset_id": f"W{index}",
        "title": f"Distinctive longitudinal Maastricht cohort dataset {index}",
        "doi": f"10.1234/um.{index}",
        "keywords": ["Medicine", "longitudinal metabolomics"],
        "raw": {
            "openalex": {
                "id": f"https://openalex.org/W{index}",
                "topics": [{"display_name": "Population Health"}],
                "related_works": [f"https://openalex.org/R{index}"],
            }
        },
    }
    values.update(kwargs)
    return UMDatasetRecord(**values)


def test_catalog_profile_describes_ordered_phases_without_semantic_search():
    profile = profile_um_catalog([record(1), record(2, doi=None, keywords=[])])

    assert profile["dataset_count"] == 2
    assert profile["coverage"]["openalex_ids"] == 100.0
    assert profile["coverage"]["dois"] == 50.0
    assert "Medicine" not in profile["top_keywords"]
    assert [phase["id"] for phase in profile["phases"]] == [
        "direct",
        "source_citations",
        "exact",
        "related",
        "focused",
    ]
    assert "semantic" not in str(profile).lower()


def test_catalog_fingerprint_changes_when_detection_aliases_change():
    first = profile_um_catalog([record(1, aliases=["TMS"])])["catalog_fingerprint"]
    second = profile_um_catalog([record(1, aliases=["Maastricht Study"])])["catalog_fingerprint"]
    assert first != second


def test_direct_queries_cover_all_work_ids_in_batches_without_pdf_or_focus_filters():
    queries = build_direct_queries(
        [record(index) for index in range(205)],
        from_year=2020,
        to_year=2025,
        publication_types=["article", "preprint"],
    )

    links = [query for query in queries if query.evidence_reason == EvidenceReason.DATASET_LINK]
    citations = [query for query in queries if query.evidence_reason == EvidenceReason.DATASET_CITATION]
    assert len(links) == 3
    assert len([query for query in citations if query.query is None]) == 3
    assert len([query for query in citations if query.query and "secondary analysis" in query.query]) == 3
    assert all(query.phase == DiscoveryPhase.DIRECT for query in queries)
    assert all(query.filters["publication_year"] == "2020-2025" for query in queries)
    assert all(query.filters["type"] == ["article", "preprint"] for query in queries)
    assert all("has_pdf_url" not in query.filters for query in queries)
    filter_batches = [
        value
        for query in queries
        for key, value in query.filters.items()
        if key in {"datasets", "cites"} and isinstance(value, list)
    ]
    assert all(len(batch) <= 100 for batch in filter_batches)


def test_direct_query_maps_structured_openalex_ids_back_to_um_catalog_ids():
    source = record(1, um_dataset_id="um-dataset-1", raw={"openalex": {"id": "W1"}})
    query = next(q for q in build_direct_queries([source]) if q.evidence_reason == EvidenceReason.DATASET_LINK)

    assert structured_direct_dataset_ids(
        {"raw": {"datasets": [{"id": "https://openalex.org/W1"}]}}, query
    ) == ("um-dataset-1",)


def test_exact_queries_batch_dois_and_titles_and_only_attribute_visible_terms():
    records = [record(index) for index in range(26)]
    queries = build_exact_queries(records)
    doi_queries = [query for query in queries if query.evidence_reason == EvidenceReason.IDENTIFIER_MENTION]
    title_queries = [query for query in queries if query.evidence_reason == EvidenceReason.TITLE_MENTION]

    assert len(doi_queries) == 2
    assert len(title_queries) == 6
    assert all((query.query or "").count(" OR ") <= 24 for query in doi_queries)
    assert all((query.query or "").count(" OR ") <= 4 for query in title_queries)
    matched = verified_exact_dataset_ids(
        {"title": "Analysis using DOI 10.1234/um.0", "abstract": "No other identifier."},
        doi_queries[0],
    )
    assert matched == ("W0",)


def test_distinctive_aliases_and_focused_queries_remove_generic_terms_and_apply_focus_only_here():
    records = [
        record(1, title="Data", aliases=["Maastricht Aging Study longitudinal cognitive assessment"], keywords=["Computer science", "cognitive aging cohort"]),
        record(2, keywords=["cognitive aging cohort"]),
    ]

    assert ("W1", "Maastricht Aging Study longitudinal cognitive assessment") in distinctive_titles(records, 10)
    assert informative_keywords(records, 10) == ["cognitive aging cohort"]
    queries = build_focused_queries(records, {"Population Health": "T1"}, focus_query="education")
    assert all(query.phase == DiscoveryPhase.FOCUSED for query in queries)
    assert all("education" in (query.query or "") for query in queries)


def test_citation_expansion_queries_batch_distinct_seeds_and_search_for_reuse():
    seeds = [f"W{i}" for i in range(101)] + ["W1"]
    queries = build_citing_queries(seeds, DiscoveryPhase.RELATED)
    assert len(queries) == 2
    for query, expected_size in zip(queries, (100, 1), strict=True):
        batch = query.filters["cites"]
        assert isinstance(batch, list)
        assert len(batch) == expected_size
    assert all(query.query and '"secondary analysis"' in query.query for query in queries)
    assert all(query.evidence_reason == EvidenceReason.RELATED_WORK_CITATION for query in queries)


def test_random_query_uses_native_seeded_sample_with_shared_filters():
    query = build_random_query(
        250,
        42,
        from_year=2020,
        to_year=2025,
        publication_types=["article", "preprint"],
    )

    assert query.phase == DiscoveryPhase.RANDOM
    assert query.evidence_reason == EvidenceReason.RANDOM_SAMPLE
    assert query.sample_size == 250
    assert query.sample_seed == 42
    assert query.sort is None
    assert query.filters == {
        "publication_year": "2020-2025",
        "type": ["article", "preprint"],
    }


def test_language_filter_is_shared_by_every_strategy_query():
    records = [record(1)]
    queries = [
        *build_direct_queries(records, language="en"),
        *build_exact_queries(records, language="en"),
        *build_citing_queries(["W101"], DiscoveryPhase.RELATED, language="en"),
        *build_focused_queries(records, {"Population Health": "T1"}, language="en"),
        build_random_query(10, 42, language="en"),
    ]
    assert queries
    assert all(query.filters["language"] == "en" for query in queries)


def test_ranking_uses_tiers_not_probability_and_reserves_ready_pool_capacity():
    candidates = rank_strategy_candidates(
        [
            {"paperId": "W900", "title": "Direct no PDF", "discovery_reasons": ["dataset_link"], "raw": {"datasets": [{"id": "W1"}]}},
            {"paperId": "W901", "title": "Ready exact", "open_access_url": "https://example.org/901.pdf", "discovery_reasons": ["identifier_mention"], "raw": {}},
            {"paperId": "W902", "title": "Ready expanded", "open_access_url": "https://example.org/902.pdf", "discovery_reasons": ["focused_keyword"], "raw": {}},
        ],
        known_dataset_ids=["W1"],
        processing_limit=1,
        discovery_limit=2,
    )

    assert [candidate["paper_id"] for candidate in candidates] == ["W900", "W901"]
    assert candidates[0]["candidate_strength"] == 90
    assert candidates[0]["evidence_tier"] == "direct"
    assert candidates[0]["included"] is False
    assert candidates[1]["included"] is True
    assert candidates[1]["candidate_strength"] == 85


def test_ranking_merges_evidence_and_caps_corroboration_inside_strongest_tier():
    candidates = rank_strategy_candidates(
        [
            {"paperId": "W900", "open_access_url": "https://example.org/paper.pdf", "discovery_reasons": ["focused_keyword"], "matched_um_dataset_ids": ["W1"], "raw": {}},
            {"paperId": "W900", "open_access_url": "https://example.org/paper.pdf", "discovery_reasons": ["identifier_mention", "title_mention"], "matched_um_dataset_ids": ["W2"], "raw": {}},
        ],
        known_dataset_ids=["W1", "W2"],
        processing_limit=1,
    )

    assert candidates[0]["matched_um_dataset_ids"] == ["W1", "W2"]
    assert candidates[0]["evidence_tier"] == "exact"
    assert candidates[0]["candidate_strength"] == 89


def test_random_candidates_keep_sample_order_instead_of_citation_order():
    candidates = rank_strategy_candidates(
        [
            {"paperId": "W10", "open_access_url": "https://example.org/10.pdf", "cited_by_count": 1_000, "random_sample_rank": 2, "discovery_reasons": ["random_sample"], "raw": {}},
            {"paperId": "W20", "open_access_url": "https://example.org/20.pdf", "cited_by_count": 0, "random_sample_rank": 1, "discovery_reasons": ["random_sample"], "raw": {}},
        ],
        known_dataset_ids=[],
        processing_limit=2,
    )

    assert [candidate["paper_id"] for candidate in candidates] == ["W20", "W10"]
    assert all(candidate["candidate_strength"] == 40 for candidate in candidates)
