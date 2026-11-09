"""Versioned API router assembly."""

from __future__ import annotations

from fastapi import APIRouter

from datasight.interfaces.api.routes import (
    admin,
    discovery,
    extractions,
    health,
    imports,
    insights,
    runs,
    stages,
    um_datasets,
)

router = APIRouter(prefix="/api/v1")
router.include_router(health.router)
router.include_router(stages.router)
router.include_router(runs.router)
router.include_router(extractions.router)
router.include_router(discovery.router)
router.include_router(insights.router)
router.include_router(imports.router)
router.include_router(um_datasets.router)
router.include_router(admin.router)

__all__ = ["router"]
