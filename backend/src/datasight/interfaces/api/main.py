"""FastAPI application factory for the DataSight backend."""

from __future__ import annotations

from fastapi import FastAPI

from datasight.config import APP_VERSION
from datasight.interfaces.api.router import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="DataSight API",
        version=APP_VERSION,
        description="Async HTTP API for running and monitoring the DataSight dataset mention pipeline.",
    )
    app.include_router(router)
    return app


app = create_app()

