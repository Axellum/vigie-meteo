"""Point d'entrée FastAPI de l'API Météo-France unifiée."""

import asyncio
import contextlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.aromeom_grid import AromeOmStore, find_window, merged_series
from app.aromepi_grid import STEP_SECONDS
from app.cache import CacheStore
from app.collectors.aromeom import ATTRIBUTION as AROMEOM_ATTRIBUTION
from app.collectors.aromeom import SOURCE_MODEL as AROMEOM_SOURCE_MODEL
from app.collectors.aromeom import AromeOmCollector
from app.collectors.aromepi import ATTRIBUTION as AROMEPI_ATTRIBUTION
from app.collectors.aromepi import SOURCE_MODEL as AROMEPI_SOURCE_MODEL
from app.collectors.aromepi import AromePiCollector, AromePiStore
from app.collectors.dpclim import ATTRIBUTION as DPCLIM_ATTRIBUTION
from app.collectors.dpclim import PROVIDER as DPCLIM_PROVIDER
from app.collectors.dpclim import SOURCE_MODEL as DPCLIM_SOURCE_MODEL
from app.collectors.dpclim import BusyError, StationDailyCollector, UpstreamError
from app.collectors.ensemble import (
    AROME_ATTRIBUTION,
    AROME_SOURCE_MODEL,
    ARPEGE_ATTRIBUTION,
    ARPEGE_SOURCE_MODEL,
    MEMBERS,
    PeAromeCollector,
    PeArpegeCollector,
    ProbabilityStore,
)
from app.collectors.forests import ATTRIBUTION as FORESTS_ATTRIBUTION
from app.collectors.forests import PROVIDER as FORESTS_PROVIDER
from app.collectors.forests import SOURCE_MODEL as FORESTS_SOURCE_MODEL
from app.collectors.forests import ForestsCollector
from app.collectors.meteofrance import (
    PIAF_ATTRIBUTION,
    PIAF_SOURCE_MODEL,
    PIAF_STEP_SECONDS,
    MeteoFranceNowcastCollector,
)
from app.collectors.open_meteo import OpenMeteoCollector
from app.collectors.radar import ATTRIBUTION as RADAR_ATTRIBUTION
from app.collectors.radar import FRESH_WITHIN_S, RadarCollector, RadarStore
from app.collectors.radar import SOURCE_MODEL as RADAR_SOURCE_MODEL
from app.config import Settings, get_settings, redact_secret
from app.models import (
    CompactWeatherResponse,
    EnsembleBlock,
    ForestDepartment,
    ForestsResponse,
    HealthResponse,
    OverseasResponse,
    OverseasStep,
    ProbabilitiesResponse,
    ProbabilityWindow,
    ProblemDetail,
    RadarFrameInfo,
    RadarFramesResponse,
    RadarLegendClass,
    RainNowcastBlock,
    RainNowcastFrameInfo,
    RainNowcastFramesResponse,
    ShortTermResponse,
    ShortTermStep,
    SourceStatus,
    StationDailyResponse,
    UnifiedWeatherResponse,
)
from app.piaf_grid import grid_bounds, piaf_tile_png
from app.radar_grid import AREAS, MAX_ZOOM, METROPOLE, MIN_ZOOM, legend, tile_png
from app.security import ApiKeyMiddleware
from app.services.weather import WeatherAggregatorService

log = logging.getLogger(__name__)


async def purge_cache_forever(cache: CacheStore, cfg: Settings) -> None:
    """Tâche de fond : vide du cache SQLite les entrées que plus rien ne peut servir (plus vieilles que la tolérance « périmé »).

    Les relevés DPClim et la météo des forêts gardent leurs entrées : leur propre logique les renouvelle (une commande par station et par jour).
    """
    while True:
        try:
            removed = await asyncio.to_thread(
                cache.purge_older_than, cfg.max_stale_seconds, (DPCLIM_PROVIDER, FORESTS_PROVIDER)
            )
            if removed:
                log.info("cache SQLite : %d entrées trop vieilles supprimées", removed)
        except sqlite3.Error as exc:  # une base occupée ou illisible : on réessaie au prochain tour
            log.warning("purge du cache SQLite en échec : %s", exc)
        await asyncio.sleep(cfg.cache_purge_interval_seconds)


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

        radar_dir = Path(cfg.radar_dir) if cfg.radar_dir else Path(cfg.db_path).resolve().parent / "radar"
        # Un collecteur par zone ; la métropole garde le dossier d'avant (`radar/`), les autres ont `radar/<zone>/`.
        wanted = [name.strip() for name in cfg.radar_areas.split(",") if name.strip() in AREAS] or [METROPOLE.slug]
        radars: dict[str, RadarCollector] = {}
        for slug in dict.fromkeys(wanted):
            area = AREAS[slug]
            directory = radar_dir if area is METROPOLE else radar_dir / area.slug
            radars[slug] = RadarCollector(
                client, RadarStore(directory, keep_seconds=cfg.radar_keep_minutes * 60, area=area), cfg, area
            )
        # La métropole reste toujours servie (ses routes d'avant) : sans image si elle n'est pas dans la liste.
        radars.setdefault(METROPOLE.slug, RadarCollector(client, RadarStore(radar_dir, keep_seconds=cfg.radar_keep_minutes * 60), cfg))
        app_instance.state.radars = radars
        app_instance.state.radar = radars[METROPOLE.slug]
        radar_tasks = [
            asyncio.create_task(radar.run_forever())
            for slug, radar in radars.items()
            if slug in wanted and radar.enabled() and own_client
        ]

        aromepi_dir = Path(cfg.aromepi_dir) if cfg.aromepi_dir else Path(cfg.db_path).resolve().parent / "aromepi"
        aromepi_store = AromePiStore(aromepi_dir, keep_runs=cfg.aromepi_keep_runs)
        aromepi = AromePiCollector(client, aromepi_store, cfg)
        app_instance.state.aromepi = aromepi
        aromepi_task = asyncio.create_task(aromepi.run_forever()) if aromepi.enabled() and own_client else None

        aromeom_dir = Path(cfg.aromeom_dir) if cfg.aromeom_dir else Path(cfg.db_path).resolve().parent / "aromeom"
        aromeom = AromeOmCollector(client, AromeOmStore(aromeom_dir, keep_runs=cfg.aromeom_keep_runs), cfg)
        app_instance.state.aromeom = aromeom
        aromeom_task = asyncio.create_task(aromeom.run_forever()) if aromeom.enabled() and own_client else None

        dpclim = StationDailyCollector(client, cache, cfg)
        app_instance.state.dpclim = dpclim
        forests = ForestsCollector(client, cache, cfg)
        app_instance.state.forests = forests
        forests_task = asyncio.create_task(forests.run_forever()) if forests.enabled() and own_client else None

        pearome_dir = Path(cfg.pearome_dir) if cfg.pearome_dir else Path(cfg.db_path).resolve().parent / "pearome"
        pearome = PeAromeCollector(client, ProbabilityStore(pearome_dir, keep_runs=cfg.pearome_keep_runs), cfg)
        app_instance.state.pearome = pearome
        pearome_task = asyncio.create_task(pearome.run_forever()) if pearome.enabled() and own_client else None
        pearp_dir = Path(cfg.pearp_dir) if cfg.pearp_dir else Path(cfg.db_path).resolve().parent / "pearp"
        pearp = PeArpegeCollector(client, ProbabilityStore(pearp_dir, keep_runs=cfg.pearp_keep_runs), cfg)
        app_instance.state.pearp = pearp
        pearp_task = asyncio.create_task(pearp.run_forever()) if pearp.enabled() and own_client else None

        purge_task = asyncio.create_task(purge_cache_forever(cache, cfg)) if own_client else None

        try:
            yield
        finally:
            for task in (pearome_task, pearp_task, aromeom_task, forests_task, purge_task):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            if aromepi_task is not None:
                aromepi_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await aromepi_task
            for radar_task in radar_tasks:
                radar_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await radar_task
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

        for slug, radar in request.app.state.radars.items():
            epochs = radar.store.epochs()
            newest = epochs[-1] if epochs else None
            if not radar.enabled():
                radar_status = "not_configured"
            elif newest is not None and datetime.now(UTC).timestamp() - newest <= FRESH_WITHIN_S:
                radar_status = "fresh"
            else:
                radar_status = "stale" if newest is not None else "unavailable"
            name = "meteofrance-radar" if slug == METROPOLE.slug else f"meteofrance-radar-{slug}"
            sources[name] = SourceStatus(
                status=radar_status,
                provider=name,
                updated_at=datetime.fromtimestamp(newest, tz=UTC) if newest is not None else None,
                age_seconds=None if newest is None else max(0, int(datetime.now(UTC).timestamp() - newest)),
                last_error=radar.last_error,
            )

        aromepi: AromePiCollector = request.app.state.aromepi
        latest_run = await run_in_threadpool(aromepi.store.latest)
        if not aromepi.enabled():
            aromepi_status = "not_configured"
        elif latest_run is not None and datetime.now(UTC) - latest_run.run <= timedelta(minutes=cfg.aromepi_max_age_minutes):
            aromepi_status = "fresh"
        else:
            aromepi_status = "stale" if latest_run is not None else "unavailable"
        sources["meteofrance-aromepi"] = SourceStatus(
            status=aromepi_status,
            provider="meteofrance-aromepi",
            updated_at=latest_run.run if latest_run is not None else None,
            age_seconds=None if latest_run is None else max(0, int((datetime.now(UTC) - latest_run.run).total_seconds())),
            last_error=aromepi.last_error,
        )

        aromeom: AromeOmCollector = request.app.state.aromeom
        om_runs = await run_in_threadpool(aromeom.store.latest_runs, 1)
        om_run = om_runs[0].run if om_runs else None
        if not aromeom.enabled():
            om_status = "not_configured"
        elif om_run is not None and datetime.now(UTC) - om_run <= timedelta(minutes=cfg.aromeom_max_age_minutes):
            om_status = "fresh"
        else:
            om_status = "stale" if om_run is not None else "unavailable"
        sources["meteofrance-aromeom"] = SourceStatus(
            status=om_status,
            provider="meteofrance-aromeom",
            updated_at=om_run,
            age_seconds=None if om_run is None else max(0, int((datetime.now(UTC) - om_run).total_seconds())),
            last_error=aromeom.last_error,
        )

        dpclim_state = cache.get_provider_state("meteofrance-dpclim")
        dpclim_collector: StationDailyCollector = request.app.state.dpclim
        if not dpclim_collector.enabled():
            dpclim_status = "not_configured"
        elif dpclim_state["circuit_open"]:
            dpclim_status = "stale"
        else:
            dpclim_status = "fresh" if dpclim_state["last_success_at"] else "unavailable"
        sources["meteofrance-dpclim"] = SourceStatus(
            status=dpclim_status,
            provider="meteofrance-dpclim",
            updated_at=dpclim_state["last_success_at"],
            circuit_open=dpclim_state["circuit_open"],
            last_error=dpclim_state["last_error"],
        )

        forests_collector: ForestsCollector = request.app.state.forests
        forests_status, forests_checked = forests_collector.health()
        sources["meteofrance-forests"] = SourceStatus(
            status=forests_status,
            provider="meteofrance-forests",
            updated_at=datetime.fromtimestamp(forests_checked, tz=UTC) if forests_checked is not None else None,
            age_seconds=None if forests_checked is None else max(0, int(datetime.now(UTC).timestamp() - forests_checked)),
            last_error=forests_collector.last_error,
        )

        for state_name, provider, max_age in (
            ("pearome", "meteofrance-pearome", cfg.pearome_max_age_minutes),
            ("pearp", "meteofrance-pearp", cfg.pearp_max_age_minutes),
        ):
            collector = getattr(request.app.state, state_name)
            newest_run = await run_in_threadpool(collector.store.latest)
            if not collector.enabled():
                ensemble_status = "not_configured"
            elif newest_run is not None and datetime.now(UTC) - newest_run.run <= timedelta(minutes=max_age):
                ensemble_status = "fresh"
            else:
                ensemble_status = "stale" if newest_run is not None else "unavailable"
            sources[provider] = SourceStatus(
                status=ensemble_status,
                provider=provider,
                updated_at=newest_run.run if newest_run is not None else None,
                age_seconds=None if newest_run is None else max(0, int((datetime.now(UTC) - newest_run.run).total_seconds())),
                last_error=collector.last_error,
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

    @app.get("/v1/rain-nowcast/frames", response_model=RainNowcastFramesResponse, tags=["Météo"])
    async def get_rain_nowcast_frames(request: Request, response: Response) -> RainNowcastFramesResponse:
        """Les échéances (+5 à +60 min) du dernier run PIAF (chargé à la demande si besoin), pour afficher la pluie prévue en tuiles PNG."""
        now_collector: MeteoFranceNowcastCollector = request.app.state.now_collector
        run = await now_collector.ensure_run()
        if run is None:
            raise HTTPException(status_code=503, detail="Aucun run PIAF chargé pour l'instant.")
        response.headers["Cache-Control"] = "public, max-age=60"
        frames = [
            RainNowcastFrameInfo(
                epoch=int(run.step_time(idx).timestamp()),
                time_utc=run.step_time(idx),
                lead_minutes=(idx + 1) * (PIAF_STEP_SECONDS // 60),
            )
            for idx in range(len(run.steps))
        ]
        return RainNowcastFramesResponse(
            source_model=PIAF_SOURCE_MODEL,
            run_utc=run.run.astimezone(UTC),
            bounds=list(grid_bounds(run.steps[0])),
            min_zoom=MIN_ZOOM,
            max_zoom=MAX_ZOOM,
            step_seconds=PIAF_STEP_SECONDS,
            # Le run dans l'adresse : un nouveau run redessine les mêmes instants, une adresse sans run ne peut pas être immuable.
            tile_path=f"/v1/rain-nowcast/tiles/{{epoch}}/{{z}}/{{x}}/{{y}}.png?run={int(run.run.timestamp())}",
            legend=[RadarLegendClass(**entry) for entry in legend()],
            frames=frames,
            attribution=PIAF_ATTRIBUTION,
        )

    @app.get("/v1/rain-nowcast/tiles/{epoch}/{z}/{x}/{y}.png", tags=["Météo"], response_class=Response)
    async def get_rain_nowcast_tile(
        request: Request,
        epoch: int,
        z: int,
        x: int,
        y: int,
        run: int | None = Query(None, description="Run PIAF (epoch UTC) lu dans `tile_path` de `/v1/rain-nowcast/frames`."),
    ) -> Response:
        """Une tuile PNG (256 px, Web Mercator, zooms 3 à 10) d'une échéance du dernier run PIAF (chargé à la demande si besoin).

        Un nouveau run toutes les 5 minutes redessine les mêmes instants : la tuile n'est « immutable » que si `run` est donné et
        est le run qui l'a dessinée ; sinon elle est gardée 60 s (le run en mémoire peut être plus récent que celui du client, ou
        plus ancien dans l'autre processus). Le run dessiné est dans l'en-tête `X-Piaf-Run`.
        """
        if not (MIN_ZOOM <= z <= MAX_ZOOM) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
            raise HTTPException(status_code=404, detail="Tuile hors de la carte.")
        now_collector: MeteoFranceNowcastCollector = request.app.state.now_collector
        piaf = await now_collector.ensure_run(required_epoch=epoch)
        if piaf is None:
            raise HTTPException(status_code=503, detail="Aucun run PIAF chargé pour l'instant.")
        grid = piaf.step_for_epoch(epoch)
        if grid is None:
            raise HTTPException(status_code=404, detail="Échéance PIAF inconnue dans le run courant.")
        png = await run_in_threadpool(piaf_tile_png, grid, z, x, y)
        drawn = int(piaf.run.timestamp())
        return Response(
            content=png,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=3600, immutable" if run == drawn else "public, max-age=60",
                "X-Piaf-Run": str(drawn),
            },
        )

    @app.get("/v1/shortterm", response_model=ShortTermResponse, tags=["Météo"])
    async def get_shortterm(
        request: Request,
        response: Response,
        lat: float = Query(..., ge=-90.0, le=90.0),
        lon: float = Query(..., ge=-180.0, le=180.0),
    ) -> ShortTermResponse:
        """Grêle, rafales, visibilité et type de précipitation des 6 prochaines heures (AROME-PI, pas de 15 minutes) autour d'un point.

        La carte du dernier run est lue en mémoire : le coût ne dépend pas du nombre de points. 404 hors de la métropole et de la Corse,
        503 sans run récent (le client garde alors sa météo habituelle)."""
        aromepi: AromePiCollector = request.app.state.aromepi
        run = await run_in_threadpool(aromepi.store.latest)
        now = datetime.now(UTC)
        if run is None or now - run.run > timedelta(minutes=cfg.aromepi_max_age_minutes):
            raise HTTPException(status_code=503, detail="Aucun run AROME-PI récent.")
        values = run.point(lat, lon)
        if values is None:
            raise HTTPException(status_code=404, detail="Point hors de la zone AROME-PI (métropole et Corse).")
        steps = []
        for k in range(len(values["gust_kmh"])):
            end = run.run + timedelta(seconds=STEP_SECONDS * (k + 1))
            if end < now:
                continue  # échéance déjà entièrement passée
            steps.append(
                ShortTermStep(
                    time=end,
                    wind_gust_kmh=values["gust_kmh"][k],
                    hail_kg_m2=values["hail_kg_m2"][k],
                    visibility_m=values["visibility_m"][k],
                    precip_type=values["precip_type"][k],
                )
            )
        if not steps:
            raise HTTPException(status_code=503, detail="Aucun run AROME-PI récent.")
        response.headers["Cache-Control"] = "public, max-age=300"
        return ShortTermResponse(source_model=AROMEPI_SOURCE_MODEL, attribution=AROMEPI_ATTRIBUTION, run=run.run, steps=steps)

    @app.get("/v1/overseas", response_model=OverseasResponse, tags=["Météo"])
    async def get_overseas(
        request: Request,
        response: Response,
        lat: float = Query(..., ge=-90.0, le=90.0),
        lon: float = Query(..., ge=-180.0, le=180.0),
    ) -> OverseasResponse:
        """Prévision horaire AROME-OM (Météo-France, outre-mer) d'un point : température, humidité, vent, rafales, pression, nébulosité et pluie, jusqu'à +48 h.

        Les dernières heures de deux runs sont recollées (le plus récent l'emporte). Le point est lu au nœud de 0,025° le plus proche. 404 hors des fenêtres
        servies (Guadeloupe, Martinique, Saint-Martin, Saint-Barthélemy, Guyane, La Réunion, Mayotte, Nouvelle-Calédonie, îles de la Société, Tuamotu de l'ouest et
        Australes), 503 sans run récent."""
        aromeom: AromeOmCollector = request.app.state.aromeom
        runs = await run_in_threadpool(aromeom.store.latest_runs, 2)
        now = datetime.now(UTC)
        window = find_window(lat, lon)
        if window is None:
            raise HTTPException(status_code=404, detail="Point hors des fenêtres AROME-OM servies.")
        if not runs or now - runs[0].run > timedelta(minutes=cfg.aromeom_max_age_minutes):
            raise HTTPException(status_code=503, detail="Aucun run AROME-OM récent.")
        series = await run_in_threadpool(merged_series, runs, lat, lon)
        node = runs[0].node_latlon(lat, lon)
        if series is None or node is None:
            raise HTTPException(status_code=404, detail="Point hors des fenêtres AROME-OM servies.")
        steps = [OverseasStep(time=when, **row) for when, row in series if when >= now - timedelta(hours=1)]
        if not steps:
            raise HTTPException(status_code=503, detail="Aucun run AROME-OM récent.")
        response.headers["Cache-Control"] = "public, max-age=900"
        return OverseasResponse(
            source_model=AROMEOM_SOURCE_MODEL,
            attribution=AROMEOM_ATTRIBUTION,
            window=window.name,
            utc_offset_s=window.utc_offset_s,
            run=runs[0].run,
            nearest_node={"lat": node[0], "lon": node[1]},
            steps=steps,
        )

    @app.get("/v1/station-daily", response_model=StationDailyResponse, tags=["Climat"])
    async def get_station_daily(
        request: Request,
        response: Response,
        station: str = Query(..., pattern=r"^\d{8}$", description="Numéro Météo-France de la station (8 chiffres), par exemple celui d'une fiche climatologique."),
        days: int = Query(7, ge=1, le=14),
    ) -> StationDailyResponse:
        """Les derniers jours mesurés par une station (température minimale et maximale, pluie, vent moyen et rafale), depuis les relevés quotidiens de Météo-France (DPClim).

        Hier n'est disponible qu'après 11 h 30 UTC. Le portail est interrogé une fois par station et par jour au plus. 503 si le portail ne répond pas
        ou si trop de commandes attendent ; une station sans mesure donne `days` vide."""
        collector: StationDailyCollector = request.app.state.dpclim
        if not collector.enabled():
            raise HTTPException(status_code=503, detail="Relevés de stations non configurés.")
        try:
            rows, last, fetched = await collector.daily(station, days)
        except BusyError as exc:
            raise HTTPException(status_code=503, detail="Trop de demandes de relevés en attente, réessayer plus tard.", headers={"Retry-After": "30"}) from exc
        except UpstreamError as exc:
            raise HTTPException(status_code=503, detail="Relevés de Météo-France indisponibles.") from exc
        response.headers["Cache-Control"] = "public, max-age=3600"
        return StationDailyResponse(
            source_model=DPCLIM_SOURCE_MODEL, attribution=DPCLIM_ATTRIBUTION, station=station, last_day=last, fetched_at=fetched, days=rows
        )

    @app.get("/v1/forests", response_model=ForestsResponse, tags=["Météo"])
    async def get_forests(
        request: Request,
        response: Response,
        department: str | None = Query(None, pattern=r"^(\d{2}|2A|2B)$", description="Code du département (`13`, `2A`...) ; absent = tous."),
    ) -> ForestsResponse:
        """La Météo des forêts (danger de feux de forêt, niveaux 1 à 4 pour J+1 et J+2) : `status` vaut `available` quand Météo-France publie (de juin à septembre), `out_of_season` sinon."""
        collector: ForestsCollector = request.app.state.forests
        if not collector.enabled():
            raise HTTPException(status_code=503, detail="Météo des forêts non configurée.")
        latest = collector.latest()
        if latest is None:
            raise HTTPException(status_code=503, detail="Météo des forêts pas encore lue.")
        payload, checked = latest
        departments = [ForestDepartment.model_validate(raw) for raw in payload["departments"]]
        if department is not None:
            departments = [d for d in departments if d.department == department]
        response.headers["Cache-Control"] = "public, max-age=600"
        return ForestsResponse(
            source_model=FORESTS_SOURCE_MODEL,
            attribution=FORESTS_ATTRIBUTION,
            status=payload["state"],
            reference_time=payload.get("reference_time"),
            checked_at=datetime.fromtimestamp(checked, tz=UTC),
            departments=departments,
        )

    @app.get("/v1/probabilities", response_model=ProbabilitiesResponse, tags=["Météo"])
    async def get_probabilities(
        request: Request,
        response: Response,
        lat: float = Query(..., ge=-90.0, le=90.0),
        lon: float = Query(..., ge=-180.0, le=180.0),
    ) -> ProbabilitiesResponse:
        """Probabilités des prévisions d'ensemble de Météo-France autour d'un point : PE-AROME (tranches de 3 h, jusqu'à +51 h : pluie, neige, rafales,
        grêle, orage) et PE-ARPEGE (24 h, jusqu'à +96 h : pluie). Les cartes du dernier run sont lues en mémoire : le coût ne dépend pas du nombre
        de points. 404 hors de la métropole et de la Corse, 503 sans run récent des deux ensembles."""
        now = datetime.now(UTC)
        blocks: dict[str, EnsembleBlock | None] = {}
        have_run = False
        for state_name, model, credit, window_hours, max_age in (
            ("pearome", AROME_SOURCE_MODEL, AROME_ATTRIBUTION, 3, cfg.pearome_max_age_minutes),
            ("pearp", ARPEGE_SOURCE_MODEL, ARPEGE_ATTRIBUTION, 24, cfg.pearp_max_age_minutes),
        ):
            collector = getattr(request.app.state, state_name)
            run = await run_in_threadpool(collector.store.latest)
            blocks[state_name] = None
            if run is None or now - run.run > timedelta(minutes=max_age):
                continue
            have_run = True
            values = run.point(lat, lon)
            if values is None:
                continue
            by_hour: dict[int, dict[str, int]] = {}
            for key, per_hour in values.items():
                for hour, pct in per_hour.items():
                    by_hour.setdefault(hour, {})[key] = pct
            windows = [
                ProbabilityWindow(time=run.run + timedelta(hours=hour), window_hours=window_hours, probability_pct=by_hour[hour])
                for hour in sorted(by_hour)
                if run.run + timedelta(hours=hour) >= now
            ]
            if windows:
                blocks[state_name] = EnsembleBlock(source_model=model, attribution=credit, run=run.run, members=MEMBERS, windows=windows)
        if blocks["pearome"] is None and blocks["pearp"] is None:
            if have_run:
                raise HTTPException(status_code=404, detail="Point hors de la zone des prévisions d'ensemble (métropole et Corse).")
            raise HTTPException(status_code=503, detail="Aucun run récent des prévisions d'ensemble.")
        response.headers["Cache-Control"] = "public, max-age=600"
        return ProbabilitiesResponse(arome=blocks["pearome"], arpege=blocks["pearp"])

    def radar_of(request: Request, slug: str) -> RadarCollector:
        radar = request.app.state.radars.get(slug)
        if radar is None:
            raise HTTPException(status_code=404, detail="Zone radar inconnue : " + ", ".join(AREAS) + ".")
        return radar

    def frames_of(request: Request, response: Response, slug: str) -> RadarFramesResponse:
        radar = radar_of(request, slug)
        epochs = radar.store.epochs()
        if not epochs:
            raise HTTPException(status_code=503, detail="Aucune image radar pour l'instant.")
        area = radar.area
        response.headers["Cache-Control"] = "public, max-age=60"
        tile_path = (
            "/v1/radar/tiles/{epoch}/{z}/{x}/{y}.png"
            if area is METROPOLE
            else f"/v1/radar/{area.slug}/tiles/{{epoch}}/{{z}}/{{x}}/{{y}}.png"
        )
        return RadarFramesResponse(
            area=area.slug,
            source_model=RADAR_SOURCE_MODEL,
            attribution=RADAR_ATTRIBUTION,
            bounds=list(area.bounds),
            min_zoom=MIN_ZOOM,
            max_zoom=MAX_ZOOM,
            tile_path=tile_path,
            legend=[RadarLegendClass(**entry) for entry in legend()],
            frames=[RadarFrameInfo(time=datetime.fromtimestamp(e, tz=UTC), epoch=e) for e in epochs],
        )

    async def tile_of(request: Request, slug: str, epoch: int, z: int, x: int, y: int) -> Response:
        radar = radar_of(request, slug)
        if not (0 <= z <= MAX_ZOOM) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
            raise HTTPException(status_code=404, detail="Tuile hors de la carte.")
        frame = await run_in_threadpool(radar.store.load, epoch)
        if frame is None:
            raise HTTPException(status_code=404, detail="Image radar inconnue ou déjà purgée.")
        png = await run_in_threadpool(tile_png, frame, z, x, y)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400, immutable"})

    @app.get("/v1/radar/frames", response_model=RadarFramesResponse, tags=["Radar"])
    async def get_radar_frames(request: Request, response: Response) -> RadarFramesResponse:
        """Les images de la mosaïque radar de la métropole (lame d'eau) gardées, toutes les 5 minutes (comme `/v1/radar/france/frames`)."""
        return frames_of(request, response, METROPOLE.slug)

    @app.get("/v1/radar/tiles/{epoch}/{z}/{x}/{y}.png", tags=["Radar"], response_class=Response)
    async def get_radar_tile(request: Request, epoch: int, z: int, x: int, y: int) -> Response:
        """Une tuile PNG (256 px, Web Mercator) d'une image radar de la métropole ; l'adresse contient la date, donc elle ne change jamais."""
        return await tile_of(request, METROPOLE.slug, epoch, z, x, y)

    @app.get("/v1/radar/{area}/frames", response_model=RadarFramesResponse, tags=["Radar"])
    async def get_radar_area_frames(request: Request, response: Response, area: str) -> RadarFramesResponse:
        """Les images d'une mosaïque radar (`france`, `antilles`, `reunion`, `new-caledonia`) gardées, toutes les 5 minutes."""
        return frames_of(request, response, area)

    @app.get("/v1/radar/{area}/tiles/{epoch}/{z}/{x}/{y}.png", tags=["Radar"], response_class=Response)
    async def get_radar_area_tile(request: Request, area: str, epoch: int, z: int, x: int, y: int) -> Response:
        """Une tuile PNG (256 px, Web Mercator) d'une image d'une mosaïque radar ; l'adresse contient la date, donc elle ne change jamais."""
        return await tile_of(request, area, epoch, z, x, y)

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
