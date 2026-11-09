"""Pipeline run event helpers."""

from __future__ import annotations

from typing import Any


def event_level(status: str) -> str:
    if status in {"failed", "error"}:
        return "error"
    if status in {"skipped", "warning", "completed_with_errors"}:
        return "warning"
    return "info"


def event_message(stage: str, status: str, metrics: dict[str, Any] | None, error: str | None) -> str:
    if error:
        return f"{stage} failed: {error}"
    message = (metrics or {}).get("message")
    if isinstance(message, str) and message:
        return message
    if status == "running":
        return f"{stage} started."
    return f"{stage} finished with status: {status}."
