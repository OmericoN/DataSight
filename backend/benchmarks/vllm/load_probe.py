"""Run the same grounded source blocks at a bounded gateway concurrency."""

from __future__ import annotations

import argparse
import csv
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from datasight.application.extraction_engine import Block, LOCATE, anchor_passage, source_blocks
from datasight.domain.extraction import LocatedPassages
from datasight.infrastructure.llm.client import GatewayClient, payload_for


def samples(export_dir: Path, source_dir: Path) -> list[tuple[str, str, tuple[str, Block]]]:
    with (export_dir / "extractions.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    output = []
    for row in rows:
        digest = row.get("source_sha256") or ""
        if not digest:
            continue
        text = (source_dir / f"{digest}.md").read_text(encoding="utf-8")
        if hashlib.sha256(text.encode()).hexdigest() != digest:
            raise ValueError(f"Source snapshot hash mismatch for {row['paper_id']}")
        blocks = source_blocks(text)
        block = next((part for part in blocks if "dataset" in text[part.start:part.end].casefold()), blocks[0])
        output.append((row["paper_id"], digest, (text, block)))
    if not output:
        raise ValueError("No source snapshots in the extraction export")
    return output


def probe(sample: tuple[str, str, tuple[str, Block]], repetition: int) -> dict[str, object]:
    paper_id, digest, (text, block) = sample
    attempts: list[dict] = []
    started = time.monotonic()
    client = GatewayClient()
    try:
        result = client.complete(
            payload_for(LOCATE, {"block_id": block.block_id, "text": text[block.start:block.end]}, LocatedPassages),
            LocatedPassages, attempts.append,
            lambda value: [anchor_passage(text, block, passage) for passage in value.passages],
        )
        status, category, passages = "successful", "", len(result.passages)
    except Exception as exc:
        status, category, passages = "failed", type(exc).__name__, ""
    finally:
        client.close()
    last = attempts[-1] if attempts else {}
    usage = (last.get("response") or {}).get("usage") or {}
    return {"paper_id": paper_id, "source_sha256": digest, "block_id": block.block_id,
            "repetition": repetition, "status": status, "error_category": category,
            "elapsed_seconds": round(time.monotonic() - started, 3), "attempts": len(attempts),
            "http_status": last.get("http_status", ""), "passages": passages,
            "prompt_tokens": usage.get("prompt_tokens", ""),
            "completion_tokens": usage.get("completion_tokens", "")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.concurrency < 1 or args.repetitions < 1:
        raise ValueError("Concurrency and repetitions must be positive")
    if args.output.exists():
        raise FileExistsError(args.output)
    cohort = samples(args.export_dir, args.source_dir)
    work = [(sample, repetition) for repetition in range(1, args.repetitions + 1)
            for sample in cohort]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(pool.map(lambda item: probe(*item), work))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print({"requests": len(rows), "successful": sum(row["status"] == "successful" for row in rows),
           "wall_seconds": round(time.monotonic() - started, 3), "output": str(args.output)})


if __name__ == "__main__":
    main()
