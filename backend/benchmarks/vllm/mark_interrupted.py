"""Close a stopped local extraction worker without discarding its checkpoints."""

import argparse

from datasight.infrastructure.persistence.repository import PipelineRepository


def mark_interrupted(run_id: int) -> int:
    with PipelineRepository() as repo:
        if not repo.extraction_lock(run_id):
            raise ValueError("Extraction is still active; stop the worker first")
        repo.cursor.execute(
            "SELECT id FROM publication_extractions WHERE pipeline_run_id=%s AND status='running'",
            (run_id,),
        )
        ids = [row["id"] for row in repo.cursor.fetchall()]
        if not ids:
            raise ValueError("No running paper extraction to mark interrupted")
        reason = "Local extraction worker stopped after checkpoint"
        for extraction_id in ids:
            repo.fail_publication_extraction(extraction_id, "interrupted", reason)
        repo.interrupt_extraction_run(run_id, reason)
        return len(ids)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    print({"run_id": args.run_id, "interrupted_extractions": mark_interrupted(args.run_id)})
