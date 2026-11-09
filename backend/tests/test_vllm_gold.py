import csv

import pytest

from benchmarks.vllm.prepare_gold import PAPERS, normalized, prepare


def test_gold_preflight_requires_all_ten_papers_and_thirty_references(tmp_path):
    assert normalized("A\u00adB\n  C") == "ab c"
    labels = tmp_path / "labels.csv"
    with labels.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["paper_id", "dataset_role", "pdf_quote"])
        writer.writeheader()
        for paper_id in PAPERS[:-1]:
            for _ in range(4):
                writer.writerow({"paper_id": paper_id, "dataset_role": "used", "pdf_quote": "quote"})
    with pytest.raises(ValueError, match="ten papers"):
        prepare(labels, tmp_path / "results")
    assert not (tmp_path / "results").exists()
