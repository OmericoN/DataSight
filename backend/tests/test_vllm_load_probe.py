import csv
import hashlib

import pytest

from benchmarks.vllm.load_probe import samples


def test_load_probe_uses_verified_snapshot_and_deterministic_block(tmp_path):
    export = tmp_path / "export"
    sources = tmp_path / "sources"
    export.mkdir()
    sources.mkdir()
    text = "Introduction without a match.\n\nWe used the Alpha dataset for analysis."
    digest = hashlib.sha256(text.encode()).hexdigest()
    (sources / f"{digest}.md").write_text(text, encoding="utf-8")
    with (export / "extractions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["paper_id", "source_sha256"])
        writer.writeheader()
        writer.writerow({"paper_id": "W1", "source_sha256": digest})
    paper_id, source_hash, (source, block) = samples(export, sources)[0]
    assert paper_id == "W1" and source_hash == digest
    assert "dataset" in source[block.start:block.end]
    (sources / f"{digest}.md").write_text("modified", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        samples(export, sources)
