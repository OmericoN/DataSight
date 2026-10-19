import csv
import io

import pytest

from datasight.application.insights import (
    serialize_discovery_candidates_csv,
    serialize_insights_csv,
    serialize_run_candidate_results_csv,
    serialize_run_insight_results_csv,
)


@pytest.mark.parametrize("value", ["=1+1", "+1+1", "-1+1", "@SUM(1)", "  =1+1", "\ttext", "\rtext", "\ntext"])
def test_csv_exports_escape_spreadsheet_formulas(value):
    for serialize, column in (
        (serialize_insights_csv, "publication_title"),
        (serialize_discovery_candidates_csv, "title"),
        (serialize_run_candidate_results_csv, "publication_title"),
        (serialize_run_insight_results_csv, "publication_title"),
    ):
        exported = serialize([{"paper_id": "W1", column: value}], [column])
        row = next(csv.DictReader(io.StringIO(exported, newline="")))
        assert row[column] == f"'{value}"


def test_csv_export_preserves_numbers_quotes_and_structured_cells():
    exported = serialize_insights_csv([
        {"publication_title": 'A "title", with punctuation', "confidence": -0.5, "evidence": {"quote": "=1+1"}},
    ], ["publication_title", "confidence", "evidence"])
    row = next(csv.DictReader(io.StringIO(exported)))
    assert row == {
        "publication_title": 'A "title", with punctuation',
        "confidence": "-0.5",
        "evidence": '{"quote":"=1+1"}',
    }
