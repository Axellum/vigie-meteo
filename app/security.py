"""Authentification optionnelle par clé API (`METEO_API_KEY`).

Supporte `X-API-Key: <clé>` et `Authorization: Bearer <clé>`.
Les routes de supervision (`/v1/health`, `/docs`, `/openapi.json`, `/redoc`) restent publiques.
"""

import hmac

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import Settings
from app.models import ProblemDetail

PUBLIC_PATHS = {"/v1/health", "/docs", "/redoc", "/openapi.json"}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Vérifie la clé API si `METEO_API_KEY` est définie dans l'environnement."""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next):
        if self._settings.api_key is None:
            return await call_next(request)

        expected = self._settings.api_key.get_secret_value().strip()
        if not expected or request.url.path in PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        provided = request.headers.get("X-API-Key")
        if not provided:
            auth = request.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()

        if not provided or not hmac.compare_digest(provided, expected):
            problem = ProblemDetail(
                type="https://httpstatuses.com/401",
                title="Unauthorized",
                status=401,
                detail="Clé API manquante ou invalide (X-API-Key ou Authorization: Bearer).",
                instance=str(request.url.path),
            )
            return JSONResponse(
                status_code=401,
                content=problem.model_dump(),
                media_type="application/problem+json",
            )

        return await call_next(request)
