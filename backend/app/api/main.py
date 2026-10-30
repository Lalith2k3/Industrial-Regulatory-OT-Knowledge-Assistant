"""FastAPI application: the HTTP face of the query side (NFR-06: no RAG logic lives here).

`create_app(services)` takes ready-made services, so tests pass fakes; `app_factory()` is what
uvicorn runs and builds the real ones from Settings.
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.query import bootstrap
from app.query.bootstrap import ApiServices
from app.query.domain.errors import ConversationNotFoundError, QueryError


def create_app(services: ApiServices, frontend_dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="Pipeline & OT Knowledge Assistant", version="0.1.0")
    app.state.services = services
    app.include_router(router)

    @app.exception_handler(ConversationNotFoundError)
    def not_found(request: Request, exc: ConversationNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(QueryError)
    def unavailable(request: Request, exc: QueryError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    if frontend_dist and (frontend_dist / "index.html").is_file():
        # The built React UI, so one process serves everything (`npm run build` in frontend/).
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


def app_factory() -> FastAPI:
    settings = get_settings()
    configure_logging()
    return create_app(bootstrap.build_api_services(settings), settings.frontend_dist)
