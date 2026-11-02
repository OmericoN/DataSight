"""Fetch and verify source PDFs for the fixed ten-paper accuracy cohort."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path

import requests

from datasight.infrastructure.ingestion.downloader import PDFDownloader

PAPERS = (
    "W4403683751", "W3176451756", "W3156520986", "W3154012465",
    "W4402917106", "W3029273470", "W3159332717", "W4308573601",
    "W4395039052", "W4402696241",
)


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).replace("\u00ad", "").split()).casefold()


def prepare(labels_path: Path, output_dir: Path) -> list[dict[str, object]]:
    if output_dir.exists():
        raise FileExistsError(output_dir)
    with labels_path.open(newline="", encoding="utf-8-sig") as handle:
        labels = [row for row in csv.DictReader(handle)
                  if row["paper_id"] in PAPERS and row["dataset_role"] in {"used", "created"}]
    counts = Counter(row["paper_id"] for row in labels)
    if set(counts) != set(PAPERS) or len(labels) < 30:
        raise ValueError("Fixed cohort does not contain ten papers and thirty genuine references")
    output_dir.mkdir(parents=True)
    downloader = PDFDownloader(output_dir=output_dir / "pdfs", delay=0)
    results: list[dict[str, object]] = []
    for paper_id in PAPERS:
        try:
            record = requests.get(f"https://api.openalex.org/works/{paper_id}", timeout=20)
            record.raise_for_status()
            work = record.json()
        except (requests.RequestException, ValueError):
            results.append({"paper_id": paper_id, "status": "metadata_unavailable",
                            "gold_references": counts[paper_id], "quote_matches": 0})
            continue
        location = work.get("best_oa_location") or {}
        url = location.get("pdf_url")
        row: dict[str, object] = {"paper_id": paper_id, "language": work.get("language"),
                                   "gold_references": counts[paper_id], "pdf_url_sha256": hashlib.sha256((url or "").encode()).hexdigest()}
        if row["language"] != "en" or not url:
            row.update({"status": "unavailable", "quote_matches": 0})
            results.append(row)
            continue
        downloaded = downloader.download_paper(paper_id, url)
        if not downloaded.success or not downloaded.filepath:
            row.update({"status": downloaded.failure_category or "download_failed", "quote_matches": 0})
            results.append(row)
            continue
        try:
            output = subprocess.run(["pdftotext", "-layout", str(downloaded.filepath), "-"],
                                    check=True, capture_output=True).stdout.decode("utf-8", errors="replace")
        except (OSError, subprocess.CalledProcessError):
            row.update({"status": "text_extraction_failed", "quote_matches": 0})
            results.append(row)
            continue
        pdf_text = normalized(output)
        matching = sum(bool(quote) and quote in pdf_text for quote in
                       (normalized(item["pdf_quote"]) for item in labels if item["paper_id"] == paper_id))
        row.update({"status": "downloaded", "pdf_sha256": downloaded.sha256,
                    "pdf_bytes": downloaded.file_size_bytes, "quote_matches": matching})
        results.append(row)
    (output_dir / "cohort.json").write_text(json.dumps({"labels_sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest(),
                                                         "papers": results}, indent=2) + "\n", encoding="utf-8")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for row in prepare(args.labels, args.output_dir):
        print(f"{row['paper_id']} {row['status']} {row['quote_matches']}/{row['gold_references']} quotes matched")
