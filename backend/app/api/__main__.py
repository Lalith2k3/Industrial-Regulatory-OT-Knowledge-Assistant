"""`python -m app.api` (run from backend/): serve the API, and the built UI if present."""

import uvicorn

from app.core.config import get_settings

settings = get_settings()
uvicorn.run("app.api.main:app_factory", factory=True, host=settings.api_host, port=settings.api_port)
