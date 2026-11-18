"""Point d'entrée FastAPI de l'API Météo-France unifiée."""

from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.cache import CacheStore
from app.collectors.meteofrance import MeteoFranceNowcastCollector
from app.collectors.open_meteo import OpenMeteoCollector
from app.config import Settings, get_settings, redact_secret
from app.models import (
    CompactWeatherResponse,
    HealthResponse,
    ProblemDetail,
    RainNowcastBlock,
    SourceStatus,
    UnifiedWeatherResponse,
)
from app.security import ApiKeyMiddleware
from app.services.weather import WeatherAggregatorService


def create_app(
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Construit et configure l'application FastAPI (facilite aussi l'injection en tests)."""
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        cache = CacheStore(cfg.db_path)
        own_client = http_client is None
        client = http_client or httpx.AsyncClient()

        om_collector = OpenMeteoCollector(client, cache, cfg)
        now_collector = MeteoFranceNowcastCollector(client, cache, cfg)
        service = WeatherAggregatorService(om_collector, now_collector)

        app_instance.state.settings = cfg
        app_instance.state.cache = cache
        app_instance.state.om_collector = om_collector
        app_instance.state.now_collector = now_collector
        app_instance.state.weather_service = service

        try:
            yield
        finally:
            cache.close()
            if own_client:
                await client.aclose()

    app = FastAPI(
        title="Météo-France Unified API",
        version=__version__,
        description=(
            "API météo résiliente pour la France : fusion Open-Meteo (AROME HD + ARPEGE) "
            "et pluie dans l'heure officielle Météo-France (PIAF), "
            "avec cache SQLite WAL et circuit breaker."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(ApiKeyMiddleware, settings=cfg)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        problem = ProblemDetail(
            type=f"https://httpstatuses.com/{exc.status_code}",
            title=str(exc.detail),
            status=exc.status_code,
            detail=redact_secret(str(exc.detail), cfg),
            instance=str(request.url.path),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=problem.model_dump(),
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        problem = ProblemDetail(
            type="https://httpstatuses.com/422",
            title="Unprocessable Entity",
            status=422,
            detail=str(exc.errors()),
            instance=str(request.url.path),
        )
        return JSONResponse(
            status_code=422,
            content=problem.model_dump(),
            media_type="application/problem+json",
        )

    @app.get("/v1/health", response_model=HealthResponse, tags=["Supervision"])
    async def health(request: Request) -> HealthResponse:
        """Vérifie l'état de la base SQLite et des circuits breakers de chaque source."""
        cache: CacheStore = request.app.state.cache
        db_ok = cache.ping()

        sources: dict[str, SourceStatus] = {}
        for provider in ("open-meteo", "meteofrance-nowcast"):
            st = cache.get_provider_state(provider)
            if provider.startswith("meteofrance") and cfg.meteofrance_api_key is None:
                status_val = "not_configured"
            elif st["circuit_open"]:
                status_val = "stale"
            else:
                status_val = "fresh" if st["last_success_at"] else "unavailable"
            sources[provider] = SourceStatus(
                status=status_val,
                provider=provider,
                updated_at=st["last_success_at"],
                circuit_open=st["circuit_open"],
                last_error=st["last_error"],
            )

        global_status = "ok" if db_ok and not sources["open-meteo"].circuit_open else "degraded"
        return HealthResponse(
            status=global_status,
            version=__version__,
            db_ok=db_ok,
            api_key_required=cfg.api_key is not None and bool(cfg.api_key.get_secret_value().strip()),
            sources=sources,
        )

    @app.get("/v1/weather", response_model=UnifiedWeatherResponse, tags=["Météo"])
    async def get_weather(
        request: Request,
        response: Response,
        lat: float = Query(..., ge=-90.0, le=90.0, description="Latitude WGS84."),
        lon: float = Query(..., ge=-180.0, le=180.0, description="Longitude WGS84."),
        days: int = Query(default=4, ge=1, le=7, description="Nombre de jours de prévision (1 à 7)."),
    ) -> UnifiedWeatherResponse:
        """Retourne les prévisions fusionnées (AROME HD + ARPEGE) et la pluie à 1h."""
        service: WeatherAggregatorService = request.app.state.weather_service
        result = await service.get_unified_weather(lat=lat, lon=lon, days=days)
        if result.sources["forecast"].status == "unavailable":
            raise HTTPException(status_code=503, detail="Source de prévision indisponible et aucun cache valide.")
        response.headers["Cache-Control"] = f"public, max-age={min(cfg.nowcast_ttl_seconds, 180)}"
        return result

    @app.get("/v1/compact", response_model=CompactWeatherResponse, tags=["Embarqué"])
    async def get_compact(
        request: Request,
        response: Response,
        lat: float = Query(..., ge=-90.0, le=90.0),
        lon: float = Query(..., ge=-180.0, le=180.0),
    ) -> CompactWeatherResponse:
        """Payload ultra-léger (< 4 Ko) optimisé pour ESP32 / LVGL / écrans domotiques."""
        service: WeatherAggregatorService = request.app.state.weather_service
        result = await service.get_compact_weather(lat=lat, lon=lon)
        response.headers["Cache-Control"] = "public, max-age=180"
        return result

    @app.get("/v1/rain-nowcast", response_model=RainNowcastBlock, tags=["Météo"])
    async def get_rain_nowcast(
        request: Request,
        lat: float = Query(..., ge=-90.0, le=90.0),
        lon: float = Query(..., ge=-180.0, le=180.0),
    ) -> RainNowcastBlock:
        """Retourne la pluie dans l'heure officielle ou le repli AROME HD 15 min."""
        service: WeatherAggregatorService = request.app.state.weather_service
        return await service.get_rain_nowcast(lat=lat, lon=lon)

    @app.get("/v1/forecast", tags=["Compatibilité Open-Meteo"])
    async def proxy_open_meteo_forecast(request: Request) -> JSONResponse:
        """Proxy compatible Open-Meteo `/v1/forecast` avec cache SQLite local.

        Le corps est celui d'Open-Meteo, inchangé. Deux en-têtes disent d'où il vient, pour qu'un client qui exige du frais
        puisse refuser une réponse périmée (servie quand Open-Meteo ne répond plus, jusqu'à `METEO_MAX_STALE_SECONDS`) :
        `X-Cache-Status` (`fresh` ou `stale`) et `X-Cache-Age` (secondes depuis la dernière lecture réussie)."""
        om_collector: OpenMeteoCollector = request.app.state.om_collector
        params = dict(request.query_params)
        if "latitude" not in params or "longitude" not in params:
            raise HTTPException(status_code=422, detail="Les paramètres 'latitude' et 'longitude' sont requis.")
        payload, status = await om_collector.proxy_raw_forecast(params)
        if payload is None:
            raise HTTPException(status_code=503, detail=f"Open-Meteo indisponible ({status.last_error}).")
        return JSONResponse(
            content=payload,
            headers={"X-Cache-Status": status.status, "X-Cache-Age": str(status.age_seconds or 0)},
        )

    @app.get("/v1/marine", tags=["Compatibilité Open-Meteo"])
    async def proxy_open_meteo_marine(request: Request) -> dict[str, Any]:
        """Proxy compatible Open-Meteo `/v1/marine` (MFWAM vagues, courants, SST) avec cache SQLite local."""
        om_collector: OpenMeteoCollector = request.app.state.om_collector
        params = dict(request.query_params)
        if "latitude" not in params or "longitude" not in params:
            raise HTTPException(status_code=422, detail="Les paramètres 'latitude' et 'longitude' sont requis.")
        payload, status = await om_collector.proxy_raw_marine(params)
        if payload is None:
            raise HTTPException(status_code=503, detail=f"Open-Meteo Marine indisponible ({status.last_error}).")
        return payload

    return app


app = create_app()
