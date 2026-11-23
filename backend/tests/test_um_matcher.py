from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.matching.um_matcher import UMCatalogMatcher, match_mention_to_um_dataset
from datasight.domain.schemas import DatasetMention, DatasetMetadata, MentionEvidence, MatchStatus, UMDatasetRecord


def test_match_exact_doi():
    mention = DatasetMention(
        publication_id="p1",
        dataset_name="Maastricht Cohort Data",
        evidence=MentionEvidence(body_quote="We used Maastricht Cohort Data."),
        metadata=DatasetMetadata(persistent_identifier="https://doi.org/10.123/example"),
    )
    record = UMDatasetRecord(
        um_dataset_id="um-1",
        title="Different title",
        doi="10.123/example",
    )

    decision = match_mention_to_um_dataset(mention, [record])

    assert decision.status == MatchStatus.MATCHED
    assert decision.um_dataset_id == "um-1"
    assert decision.match_method == "exact_pid"


def test_anchored_paper_alias_matches_um_collection_title():
    mention = DatasetMention(publication_id="p", dataset_name="LUNG1", aliases=["NSCLC-Radiomics"],
                             evidence=MentionEvidence(body_quote="LUNG1 (NSCLC-Radiomics)"))
    records = [
        UMDatasetRecord(um_dataset_id="W6977944434", title="Data From NSCLC-Radiomics"),
        UMDatasetRecord(um_dataset_id="W6921838597", title="Data From NSCLC-Radiomics-Genomics"),
    ]
    decision = match_mention_to_um_dataset(mention, records)
    assert decision.status == MatchStatus.POSSIBLE
    assert decision.um_dataset_id == "W6977944434"
    assert decision.review_required


def test_multiple_grounded_links_support_one_record_or_report_conflicting_records():
    mention = DatasetMention(
        publication_id="p", dataset_name="Collection", evidence=MentionEvidence(body_quote="Deposit"),
        metadata=DatasetMetadata(dataset_identifiers=["hdl:1234/example", "10.5555/data"],
                                 dataset_urls=["https://example.org/download", "https://example.org/data"]),
    )
    doi = UMDatasetRecord(um_dataset_id="A", title="Data", doi="10.5555/data")
    url = UMDatasetRecord(um_dataset_id="B", title="Other", url="https://example.org/data")
    assert match_mention_to_um_dataset(mention, [url]).match_method == "exact_url"
    assert match_mention_to_um_dataset(mention, [doi]).match_method == "exact_pid"
    for records in ([doi, url], [url, doi]):
        match = match_mention_to_um_dataset(mention, records)
        assert match.status == MatchStatus.REVIEW_REQUIRED
        assert match.um_dataset_id is None
        assert match.candidate_um_dataset_ids == ["A", "B"]


def test_match_possible_title_creator_year():
    mention = DatasetMention(
        publication_id="p2",
        dataset_name="Maastricht Health Survey",
        evidence=MentionEvidence(body_quote="The Maastricht Health Survey dataset was used."),
        metadata=DatasetMetadata(dataset_authors=["Jane Doe"], dataset_year=2024),
    )
    record = UMDatasetRecord(
        um_dataset_id="um-2",
        title="Maastricht Health Survey Data",
        creators=["Jane Doe"],
        year=2024,
    )

    decision = match_mention_to_um_dataset(mention, [record])

    assert decision.status == MatchStatus.POSSIBLE
    assert decision.review_required is True
    assert "title_or_alias" in decision.matched_fields


def test_resolver_links_match_across_fields_and_preserve_url_path_case():
    matcher = UMCatalogMatcher([
        UMDatasetRecord(um_dataset_id="doi", title="A", doi="10.5555/A"),
        UMDatasetRecord(um_dataset_id="handle", title="B", url="https://hdl.handle.net/1234/Ab"),
        UMDatasetRecord(um_dataset_id="page", title="C", url="https://repo.test/datasets/Ab?file=A"),
        UMDatasetRecord(um_dataset_id="repo", title="D", url="https://repo.test/"),
    ])
    def decide(**fields):
        return matcher.match(DatasetMention(publication_id="p", dataset_name="Unknown",
            evidence=MentionEvidence(body_quote="Data available here"), metadata=DatasetMetadata(**fields)))

    assert decide(dataset_urls=["https://dx.doi.org/10.5555/a"]).um_dataset_id == "doi"
    assert decide(dataset_identifiers=["hdl:1234/Ab"]).um_dataset_id == "handle"
    assert decide(dataset_urls=["https://REPO.test/datasets/Ab?file=A"]).um_dataset_id == "page"
    for url in ("https://repo.test/datasets/ab?file=A", "https://repo.test/datasets/Ab?file=a",
                "https://repo.test/", "https://hdl.handle.net/1234/ab"):
        assert decide(dataset_urls=[url]).status == MatchStatus.NO_MATCH
    conflict = decide(dataset_identifiers=["hdl:1234/Ab"], dataset_urls=["https://doi.org/10.5555/a"])
    assert conflict.candidate_um_dataset_ids == ["doi", "handle"]


def test_conflicting_extracted_dois_cannot_be_confirmed_by_one_catalog_hit():
    features = {"identity": {"identifiers": {"state": "conflicting", "values": [
        {"value": {"scheme": "doi", "target": "dataset", "literal": literal}, "evidence_ids": ["e1"]}
        for literal in ("10.5555/a", "10.5555/b")
    ]}}}
    mention = DatasetMention(publication_id="p", dataset_name="Unknown",
        evidence=MentionEvidence(body_quote="Deposit"), metadata=DatasetMetadata(features=features))
    decision = match_mention_to_um_dataset(mention, [UMDatasetRecord(um_dataset_id="A", title="A", doi="10.5555/a")])
    assert decision.status == MatchStatus.REVIEW_REQUIRED and decision.um_dataset_id is None
    assert decision.candidate_um_dataset_ids == ["A"]


def test_secondary_grounded_name_survives_consolidation_into_one_mention():
    observation = {"state": "reported", "values": [{"value": name, "evidence_ids": ["e1"]}
                   for name in ("Study nickname", "Valley cohort")]}
    mention = DatasetMention(publication_id="p", dataset_name="Study nickname",
        evidence=MentionEvidence(body_quote="Study nickname"), metadata=DatasetMetadata(
            features={"identity": {"name": observation}}))
    decision = match_mention_to_um_dataset(mention, [UMDatasetRecord(um_dataset_id="v", title="Data From: Valley cohort")])
    assert decision.status == MatchStatus.POSSIBLE and decision.um_dataset_id == "v"


def test_exact_title_is_reviewable_without_fabricated_metadata():
    mention = DatasetMention(
        publication_id="p2",
        dataset_name="Maastricht Health Survey",
        evidence=MentionEvidence(body_quote="We used the Maastricht Health Survey dataset."),
    )
    record = UMDatasetRecord(
        um_dataset_id="um-2",
        title="Maastricht Health Survey",
    )

    decision = match_mention_to_um_dataset(mention, [record])

    assert decision.status == MatchStatus.POSSIBLE
    assert decision.match_method == "exact_title_or_alias"
    assert decision.review_required is True


def test_ambiguous_metadata_match_returns_all_candidate_ids_without_selection():
    mention = DatasetMention(
        publication_id="p3",
        dataset_name="Versioned Dataset",
        evidence=MentionEvidence(body_quote="We used the Versioned Dataset."),
        metadata=DatasetMetadata(dataset_authors=["Jane Doe"], dataset_year=2024),
    )
    records = [
        UMDatasetRecord(
            um_dataset_id="W1", title="Versioned Dataset", creators=["Jane Doe"], year=2024
        ),
        UMDatasetRecord(
            um_dataset_id="W2", title="Versioned Dataset", creators=["Jane Doe"], year=2024
        ),
    ]

    decision = match_mention_to_um_dataset(mention, records)

    assert decision.status == MatchStatus.REVIEW_REQUIRED
    assert decision.um_dataset_id is None
    assert decision.candidate_um_dataset_ids == ["W1", "W2"]
    assert decision.match_method == "ambiguous_exact_title_or_alias"
