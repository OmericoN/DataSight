from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datasight.application.pipeline_services import load_um_dataset_records
from datasight.infrastructure.ingestion.openalex_exports import load_um_openalex_export_bundle


def test_load_um_openalex_export_directory(tmp_path):
    (tmp_path / "OPEN_ALEX_DATA.csv").write_text(
        "PURE_ID,OPAL_ID,URL,OPAL_DOI,OPAL_TITLE,OPAL_YEAR,OPAL_KEYWORDS,OPAL_PRIMARY_SOURCE_DISPLAY_NAME\n"
        "7,https://openalex.org/W123,https://example.org,https://doi.org/10.1234/demo,Demo Dataset Work,2024,cohort;biobank,Dataverse\n",
        encoding="utf-8",
    )
    (tmp_path / "OPEN_ALEX_DATA_KEYWORDS.csv").write_text(
        "OPENALEX_ID,KEYWORD,SCORE\n"
        "https://openalex.org/W123,genomics,0.8\n",
        encoding="utf-8",
    )
    (tmp_path / "OPEN_ALEX_DATA_TOPICS.csv").write_text(
        "OPENALEX_ID,TOPIC,TOPIC_ID,SCORE,TOPIC_PRIMARY\n"
        "https://openalex.org/W123,Health data,T42,0.9,1\n",
        encoding="utf-8",
    )
    (tmp_path / "OPEN_ALEX_DATA_MESHS.csv").write_text(
        "OPENALEX_ID,MESH,QUALIFIER\n"
        "https://openalex.org/W123,Humans,analysis\n",
        encoding="utf-8",
    )
    (tmp_path / "OPEN_ALEX_DATA_RELATED_WORKS.csv").write_text(
        "OPENALEX_ID,RELATED_WORK_ID\n"
        "https://openalex.org/W123,https://openalex.org/W999\n",
        encoding="utf-8",
    )

    records = load_um_dataset_records(str(tmp_path))

    assert len(records) == 1
    record = records[0]
    assert record.um_dataset_id == "W123"
    assert record.title == "Demo Dataset Work"
    assert record.doi == "10.1234/demo"
    assert record.aliases == []
    assert record.year == 2024
    assert record.repository == "Dataverse"
    assert record.keywords == ["cohort", "biobank", "genomics"]
    assert record.raw["openalex"]["topics"][0]["topic_id"] == "T42"
    assert "mesh" not in record.raw["openalex"]
    assert record.raw["openalex"]["related_works"] == ["https://openalex.org/W999"]


def test_load_cp1252_tsv_with_singular_affiliations_and_quality_warnings(tmp_path):
    main = (
        "OPENALEX_ID\tOPAL_DOI\tOPAL_TITLE\tOPAL_YEAR\tOPAL_BEST_OA_IS_LAND_URL\t"
        "OPAL_PRIMARY_SOURCE_DISPLAY_NAME\n"
        "https://openalex.org/W123\t10.1234/cafe\tCafé dataset\t2024\t"
        "https://example.org/dataset\tDataverse\n"
        "https://openalex.org/W124\t10.5281/zenodo.***\tNULL\t2025\tNULL\tZenodo\n"
    )
    (tmp_path / "OPEN_ALEX_DATA.csv").write_bytes(main.encode("cp1252"))
    affiliations = (
        "OPENALEX_ID\tAUTH_INDEX\tOPENALEX_AUTHOR_ID\tOPENALEX_AUTHOR\tOPENALEX_ORCID\t"
        "OPENALEX_IS_CORRESPONDING\tOPENALEX_INST_ID\tOPENALEX_INST_NAME\t"
        "OPENALEX_INST_ROR\tOPENALEX_INST_COUNTRY_CODE\tOPENALEX_INST_TYPE\n"
        "https://openalex.org/W123\t0\thttps://openalex.org/A1\tJosé Author\tNULL\t1\t"
        "https://openalex.org/I34352273\tMaastricht University\t"
        "https://ror.org/02jz4aj89\tNL\teducation\n"
        "https://openalex.org/W123\t0\thttps://openalex.org/A1\tJosé Author\tNULL\t1\t"
        "https://openalex.org/I2\tPartner Institute\thttps://ror.org/012345678\tNL\teducation\n"
    )
    (tmp_path / "OPEN_ALEX_AFFILIATION.csv").write_bytes(affiliations.encode("cp1252"))
    (tmp_path / "OPEN_ALEX_DATA_CONCEPTS.csv").write_text(
        "concepts are deprecated !\n", encoding="utf-8"
    )
    (tmp_path / "OPEN_ALEX_DATA_MESHS.csv").write_text("none\n", encoding="utf-8")

    bundle = load_um_openalex_export_bundle(tmp_path)

    assert len(bundle.records) == 2
    first, second = bundle.records
    assert first.title == "Café dataset"
    assert first.creators == ["José Author"]
    assert first.url == "https://example.org/dataset"
    assert len(first.raw["openalex"]["authorships"][0]["institutions"]) == 2
    assert first.raw["openalex"]["um_affiliation"] == {
        "strict_match": True,
        "institution_ids": ["I34352273"],
        "rors": ["02jz4aj89"],
    }
    assert second.title == "Untitled dataset (W124)"
    assert second.doi is None
    assert second.raw["quality"]["missing_title"] is True
    assert bundle.metrics["fallback_title_count"] == 1
    assert bundle.metrics["invalid_doi_count"] == 1
    assert bundle.metrics["encodings"]["data"] == "cp1252"
    assert bundle.metrics["delimiters"]["data"] == "tab"
    assert all("mesh" not in warning.casefold() for warning in bundle.warnings)


def test_supplied_um_catalog_has_expected_shape():
    catalog = ROOT.parent / "data" / "um_dataset"
    if not catalog.exists():
        pytest.skip("The local UM catalog is not included in repository checkouts.")

    bundle = load_um_openalex_export_bundle(catalog)

    assert len(bundle.records) == 2748
    assert bundle.metrics["child_rows"]["affiliations"] == 25546
    assert bundle.metrics["orphan_child_rows"] == 0
    assert bundle.metrics["fallback_title_count"] == 4
    assert bundle.metrics["invalid_doi_count"] == 1
    assert bundle.metrics["duplicate_title_groups"] == 476
    assert bundle.metrics["duplicate_title_rows"] == 1103
    assert "mesh" not in bundle.metrics["child_rows"]
    assert all("mesh" not in warning.casefold() for warning in bundle.warnings)
