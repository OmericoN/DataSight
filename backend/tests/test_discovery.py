from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.domain.discovery import (
    DiscoveryOptions,
    build_discovery_profile,
    build_openalex_discovery_queries,
    dedupe_and_score_publications,
)
from datasight.domain.schemas import UMDatasetRecord


def test_build_discovery_profile_from_um_openalex_metadata():
    record = UMDatasetRecord(
        um_dataset_id="W123",
        title="Demo Dataset",
        keywords=["cohort"],
        raw={
            "openalex": {
                "id": "https://openalex.org/W123",
                "topics": [{"id": "https://openalex.org/T42", "display_name": "Health data"}],
                "keywords": [{"keyword": "biobank"}],
                "concepts": [{"concept": "Epidemiology"}],
                "related_works": ["https://openalex.org/W999"],
            }
        },
    )

    profile = build_discovery_profile([record])

    assert profile.topic_ids == ("T42",)
    assert "cohort" in profile.keyword_terms
    assert "biobank" in profile.keyword_terms
    assert profile.related_work_ids == ("W999",)


def test_build_openalex_discovery_queries_expands_topics_and_terms():
    profile = build_discovery_profile(
        [
            UMDatasetRecord(
                um_dataset_id="W123",
                title="Demo Dataset",
                keywords=["cohort"],
                raw={"openalex": {"topics": [{"topic_id": "T42"}]}},
            )
        ]
    )
    options = DiscoveryOptions(keyword_terms=("survey",), from_year=2020, to_year=2024)

    queries = build_openalex_discovery_queries("Maastricht data", options, profile, open_access_only=True)

    assert queries[0].filters == {"open_access.is_oa": True, "publication_year": "2020-2024"}
    assert any(query.reason == "topic_profile" and query.filters["topics.id"] == ("T42",) for query in queries)
    assert any(query.reason == "term_profile" and "survey" in str(query.query) for query in queries)


def test_build_openalex_discovery_queries_batches_full_catalog_relationships():
    records = [
        UMDatasetRecord(
            um_dataset_id=f"W{index}",
            title=f"Dataset {index}",
            raw={"openalex": {"id": f"https://openalex.org/W{index}"}},
        )
        for index in range(2748)
    ]
    profile = build_discovery_profile(records)

    queries = build_openalex_discovery_queries("reuse", DiscoveryOptions(), profile)
    citation_queries = [query for query in queries if query.reason == "um_dataset_citation"]
    dataset_queries = [query for query in queries if query.reason == "um_dataset_link"]

    assert len(citation_queries) == 28
    assert len(dataset_queries) == 28
    assert all(
        isinstance(query.filters["cites"], tuple) and len(query.filters["cites"]) <= 100
        for query in citation_queries
    )
    assert all(
        isinstance(query.filters["datasets"], tuple) and len(query.filters["datasets"]) <= 100
        for query in dataset_queries
    )
    assert sum(len(query.seed_work_ids) for query in citation_queries) == 2748


def test_dedupe_and_score_publications_prefers_profile_overlap():
    options = DiscoveryOptions(topic_ids=("T42",), keyword_terms=("cohort",))
    publications = [
        {
            "paperId": "W1",
            "title": "Unrelated",
            "raw": {"topics": [], "keywords": []},
            "cited_by_count": 1,
        },
        {
            "paperId": "W1",
            "title": "Maastricht cohort reuse",
            "open_access_url": "https://example.org/a.pdf",
            "has_fulltext": True,
            "raw": {
                "topics": [{"id": "https://openalex.org/T42"}],
                "keywords": [{"display_name": "cohort"}],
            },
            "cited_by_count": 100,
        },
    ]

    scored = dedupe_and_score_publications(publications, "Maastricht cohort", options)

    assert len(scored) == 1
    assert scored[0]["title"] == "Maastricht cohort reuse"
    assert scored[0]["discovery_score"] > 0.7


def test_profile_search_uses_known_works_as_citation_seeds_and_excludes_source_identities():
    title = "Voting behaviour across parliamentary elections"
    profile = build_discovery_profile([UMDatasetRecord(
        um_dataset_id="local-1", title=f"Replication files for: {title}", doi="10.1234/data",
        raw={"openalex": {"id": "W1", "related_works": ["W2", "W3"]}},
    )], max_terms=1)
    queries = build_openalex_discovery_queries("", DiscoveryOptions(), profile)
    assert any(q.reason == "related_work_citation" and q.filters["cites"] == ("W2",) for q in queries)
    rows = [
        {"paperId": "W1"}, {"paperId": "W2"}, {"paperId": "W3"},
        {"paperId": "W4", "doi": "DOI:10.1234/DATA"},
        {"paperId": "W5", "title": title.upper()},
        {"paperId": "W6", "title": f"Reassessing {title}", "discovery_reasons": ["um_dataset_reuse_search"]},
    ]
    result = dedupe_and_score_publications(rows, "", DiscoveryOptions(), profile)
    assert [row["paperId"] for row in result] == ["W6"]


def test_direct_um_relationships_outrank_terms_and_catalog_seeds_are_excluded():
    profile = build_discovery_profile(
        [
            UMDatasetRecord(
                um_dataset_id="WSEED",
                title="Known Dataset",
                keywords=["cohort"],
                raw={"openalex": {"id": "https://openalex.org/WSEED"}},
            )
        ]
    )
    publications = [
        {"paperId": "WSEED", "title": "Known Dataset", "raw": {}},
        {
            "paperId": "WCITE",
            "title": "A reuse paper",
            "raw": {"referenced_works": ["https://openalex.org/WSEED"]},
            "discovery_reasons": ["um_dataset_citation"],
        },
        {
            "paperId": "WTERM",
            "title": "A cohort analysis",
            "raw": {"keywords": [{"display_name": "cohort"}]},
            "discovery_reasons": ["term_profile"],
        },
    ]

    scored = dedupe_and_score_publications(
        publications, "cohort", DiscoveryOptions(keyword_terms=("cohort",)), profile
    )

    assert [publication["paperId"] for publication in scored] == ["WCITE", "WTERM"]
    assert scored[0]["raw"]["datasight_discovery"]["matched_um_dataset_ids"] == ["WSEED"]
    assert scored[0]["raw"]["datasight_discovery"]["reasons"] == ["um_dataset_citation"]
