"""Reset services for database state and generated runtime storage."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from datasight.config import EXTRACTIONS_DIR, EXPORTS_DIR, LOGS_DIR, MARKDOWN_DIR, PDF_DIR, STORAGE_DIR, XML_DIR
from datasight.infrastructure.persistence.repository import PipelineRepository

RESET_CONFIRMATION = "RESET DATASIGHT"
GENERATED_DIRECTORIES: tuple[Path, ...] = (PDF_DIR, XML_DIR, MARKDOWN_DIR, EXPORTS_DIR, LOGS_DIR, EXTRACTIONS_DIR)


class ResetBlockedError(RuntimeError):
    """Raised when a reset is requested while active runs exist."""


def reset_everything(force: bool = False) -> dict[str, Any]:
    with PipelineRepository() as repo:
        active_runs = repo.active_run_count()
        if active_runs and not force:
            raise ResetBlockedError(f"{active_runs} pipeline run(s) are still active.")
        truncated_tables = repo.reset_database()

    deleted_paths = reset_generated_storage()
    return {
        "status": "successful",
        "active_runs": active_runs,
        "truncated_tables": truncated_tables,
        "deleted_paths": deleted_paths,
        "recreated_directories": [str(path) for path in GENERATED_DIRECTORIES],
    }


def reset_generated_storage(directories: tuple[Path, ...] = GENERATED_DIRECTORIES) -> list[str]:
    deleted: list[str] = []
    storage_root = STORAGE_DIR.resolve()
    for directory in directories:
        resolved = directory.resolve()
        if not _is_within_storage(resolved, storage_root):
            raise ValueError(f"Refusing to reset path outside storage: {directory}")
        if resolved.exists():
            shutil.rmtree(resolved)
            deleted.append(str(resolved))
        resolved.mkdir(parents=True, exist_ok=True)
    return deleted


def _is_within_storage(path: Path, storage_root: Path) -> bool:
    return path == storage_root or storage_root in path.parents
