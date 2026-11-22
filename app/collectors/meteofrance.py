"""Collecteur officiel Météo-France : pluie dans l'heure PIAF.

Les vigilances et les observations 6 min sont lues par Vigie elle-même (vigie-api) : elles ne passent plus par
cette passerelle (retirées le 16/11/2026).

PIAF est lu **par carte, pas par point** (17/11/2026) : chaque run (toutes les 5 min) est téléchargé une fois, une
requête WCS par échéance pour toute la France (GRIB2, 3,5 Mo chacune), puis chaque point est lu dans la carte gardée
en mémoire. Avant, chaque point coûtait une requête par échéance (~13 à 18) sur un quota de 50/min : Vigie devait se
limiter à 8 points par 5 min. Le coût ne dépend plus du nombre de points.

Protégé par cache SQLite WAL + Circuit Breaker pour absorber les erreurs 429/502
du portail officiel Météo-France sans jamais faire échouer l'API cliente.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.cache import CacheStore
from app.collectors.base import ResilientCollector, snap_coord
from app.collectors.grib2 import LatLonGrid, decode_latlon_simple
from app.config import Settings, redact_secret
from app.models import (
    RainNowcastBlock,
    RainNowcastSlice,
    SourceStatus,
)

log = logging.getLogger(__name__)


def _parse_dt(raw: str | int | float | None) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class _MeteoFranceAuthCollector(ResilientCollector):
    """Base commune gérant l'en-tête d'authentification `apikey` WSO2 Météo-France."""

    def _auth_headers(self) -> dict[str, str] | None:
        if self._settings.meteofrance_api_key is None:
            return None
        token = self._settings.meteofrance_api_key.get_secret_value().strip()
        if not token:
            return None
        # WSO2 (Météo-France) rejette avec 401 si Authorization: Bearer est envoyé avec un token apiKey
        return {"apikey": token, "accept": "*/*"}


def _opt_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


PIAF_SOURCE_MODEL = "meteofrance-piaf"
PIAF_ATTRIBUTION = "Source : Météo-France, prévision immédiate PIAF (portail-api.meteofrance.fr)"
PIAF_STEP_SECONDS = 300
PIAF_FORMAT = "application/wmo-grib"
_PIAF_COVERAGE = "TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE___{run}_PT5M"


def piaf_run_candidates(now: datetime, publish_delay_minutes: int, count: int = 3) -> list[datetime]:
    """Runs PIAF à essayer, du plus récent au plus ancien (un run toutes les 5 min, publié en retard)."""
    ref = now.astimezone(UTC) - timedelta(minutes=publish_delay_minutes)
    ref = ref.replace(minute=ref.minute - ref.minute % 5, second=0, microsecond=0)
    return [ref - timedelta(minutes=5 * i) for i in range(count)]


def piaf_coverage_id(run: datetime) -> str:
    """Identifiant WCS d'un run PIAF (ex: `…___2026-11-16T20.25.00Z_PT5M`)."""
    return _PIAF_COVERAGE.format(run=run.astimezone(UTC).strftime("%Y-%m-%dT%H.%M.%SZ"))


def piaf_mm_5min(value: float) -> float | None:
    """Un cumul PIAF de 5 min lu dans la carte, ou None s'il est absent ou invalide."""
    v = _opt_float(value)
    if v is None or math.isnan(v) or v < 0.0 or v > 500.0:
        return None
    return v


def rain_slice_from_mm_5min(at: datetime, mm_5min: float) -> RainNowcastSlice:
    """Convertit un cumul 5 min en tranche ; seuils identiques au repli AROME 15 min (en mm/h)."""
    mm_h = round(mm_5min * 12.0, 2)
    if mm_h < 0.2:
        level, label = 1, "Temps sec"
    elif mm_h < 1.6:
        level, label = 2, "Pluie faible"
    elif mm_h < 6.0:
        level, label = 3, "Pluie modérée"
    else:
        level, label = 4, "Pluie forte"
    return RainNowcastSlice(time=at, rain_level=level, rain_label=label, rain_mm_h=mm_h)


@dataclass
class PiafRun:
    """Les cartes d'un run PIAF : `steps[k]` = cumul (mm) des 5 min qui finissent à `run + (k+1) x 5 min`."""

    run: datetime
    steps: list[LatLonGrid]
    loaded_at: float = field(default_factory=time.time)

    def point(self, lat: float, lon: float) -> list[float] | None:
        """Les cumuls d'un point, échéance par échéance, ou None s'il est hors de la carte."""
        idx = self.steps[0].index(lat, lon)
        if idx is None:
            return None
        j, i = idx
        return [float(grid.values[j, i]) for grid in self.steps]

    def step_time(self, index: int) -> datetime:
        """Fin des 5 minutes de cumul (UTC) pour l'échéance `index` (0 = +5 min)."""
        return self.run.astimezone(UTC) + timedelta(seconds=PIAF_STEP_SECONDS * (index + 1))

    def step_for_epoch(self, epoch: int) -> LatLonGrid | None:
        """La grille de l'échéance dont la fin de cumul vaut `epoch` (secondes UNIX UTC), ou None."""
        for index, grid in enumerate(self.steps):
            if int(self.step_time(index).timestamp()) == epoch:
                return grid
        return None


class PiafOutsideError(LookupError):
    """Le point est hors de la carte PIAF (France métropolitaine) : ce n'est pas une panne de la source."""


class MeteoFranceNowcastCollector(_MeteoFranceAuthCollector):
    """Pluie dans l'heure officielle : PIAF, pas de 5 min, lue dans la carte du dernier run."""

    provider_name = "meteofrance-nowcast"

    def __init__(self, client: httpx.AsyncClient, cache: CacheStore, settings: Settings) -> None:
        super().__init__(client, cache, settings)
        self._run: PiafRun | None = None
        self._lock = asyncio.Lock()
        self._refresh: asyncio.Task[None] | None = None

    def current_run(self) -> PiafRun | None:
        """Le run PIAF actuellement en mémoire (sans requête réseau), ou None s'il n'est pas encore chargé."""
        if self._run is None or not self._run.steps:
            return None
        return self._run

    async def ensure_run(
        self,
        now: datetime | None = None,
        *,
        required_epoch: int | None = None,
    ) -> PiafRun | None:
        """Retourne un run PIAF prêt à servir les frames/tuiles, en le chargeant à la demande si nécessaire.

        - Sans run en mémoire : charge le dernier run publié (si la clé API est configurée et le circuit fermé).
        - Avec un run périmé (`age >= piaf_ttl_seconds`) : rafraîchit en tâche de fond sous
          `piaf_grid_max_stale_seconds`, ou de façon bloquante au-delà (comme `_current_run`).
        - Avec `required_epoch` aligné sur 5 min et postérieur à la dernière échéance du run en mémoire
          (second worker uvicorn en retard sur le premier) : attend le chargement d'un run plus récent via `_load`
          (verrouillé, donc coalescé entre requêtes de tuiles concurrentes).

        Ne lève jamais : retourne le run en mémoire encore utilisable, ou None en cas d'indisponibilité.
        """
        ref_now = now or datetime.now(tz=UTC)
        current = self.current_run()
        age = (time.time() - current.loaded_at) if current is not None else None

        needs_newer_epoch = False
        if (
            current is not None
            and required_epoch is not None
            and required_epoch % PIAF_STEP_SECONDS == 0
            and current.step_for_epoch(required_epoch) is None
        ):
            last_epoch = int(current.step_time(len(current.steps) - 1).timestamp())
            max_plausible = int(
                (ref_now + timedelta(seconds=self._settings.piaf_steps * PIAF_STEP_SECONDS)).timestamp()
            )
            if last_epoch < required_epoch <= max_plausible:
                needs_newer_epoch = True

        # Cas rapide : un run frais est déjà en mémoire et couvre la demande.
        if current is not None and age is not None and age < self._settings.piaf_ttl_seconds and not needs_newer_epoch:
            return current

        fallback = (
            current
            if current is not None and age is not None and age < self._settings.piaf_grid_max_stale_seconds
            else None
        )

        headers = self._auth_headers()
        if headers is None or self._cache.is_circuit_open(self.provider_name):
            return fallback

        try:
            if needs_newer_epoch:
                loaded = await self._load(headers, ref_now, seen=current)
            else:
                loaded = await self._current_run(headers, ref_now)
        except (httpx.HTTPError, ValueError) as exc:
            err_msg = redact_secret(str(exc), self._settings)
            self._cache.record_failure(
                self.provider_name,
                err_msg,
                self._settings.circuit_fail_threshold,
                self._settings.circuit_cooldown_seconds,
            )
            log.warning("chargement PIAF à la demande en échec : %s", err_msg)
            return fallback

        if not loaded.steps:
            return fallback
        if loaded is not current:
            self._cache.record_success(self.provider_name)
        return loaded

    async def get_rain_nowcast(
        self,
        lat: float,
        lon: float,
        now: datetime | None = None,
    ) -> tuple[RainNowcastBlock, SourceStatus]:
        """Récupère les tranches de pluie 5 min pour un point GPS aligné sur la grille PIAF (0,01°)."""
        headers = self._auth_headers()
        if headers is None:
            status = SourceStatus(status="not_configured", provider=self.provider_name)
            return RainNowcastBlock(status="not_configured", slices=[]), status

        s_lat = snap_coord(lat, step=0.01)
        s_lon = snap_coord(lon, step=0.01)
        cache_key = f"mf:piaf:{s_lat}:{s_lon}"
        now_ts = time.time()
        cached = self._cache.get_cache(cache_key)
        if cached is not None:
            payload, updated_ts = cached
            age = max(0, int(now_ts - updated_ts))
            if age < self._settings.piaf_ttl_seconds:
                return self._block(payload, self._build_status("fresh", updated_ts, age))

        if self._cache.is_circuit_open(self.provider_name):
            payload, status = self._fallback_from_cache(cached, now_ts)
            return self._block(payload, status)

        try:
            piaf = await self._current_run(headers, now or datetime.now(tz=UTC))
            slices = self._slices(piaf, s_lat, s_lon)
        except PiafOutsideError:
            status = SourceStatus(status="unavailable", provider=self.provider_name, last_error="hors de la zone PIAF")
            return RainNowcastBlock(status="unavailable", slices=[]), status
        except (httpx.HTTPError, ValueError) as exc:
            err_msg = redact_secret(str(exc), self._settings)
            self._cache.record_failure(
                self.provider_name,
                err_msg,
                self._settings.circuit_fail_threshold,
                self._settings.circuit_cooldown_seconds,
            )
            payload, status = self._fallback_from_cache(cached, now_ts, last_error=err_msg)
            return self._block(payload, status)

        payload = [s.model_dump(mode="json") for s in slices]
        updated_ts = self._cache.set_cache(cache_key, self.provider_name, payload)
        self._cache.record_success(self.provider_name)
        return self._block(payload, self._build_status("fresh", updated_ts, 0))

    def _block(self, payload: Any, status: SourceStatus) -> tuple[RainNowcastBlock, SourceStatus]:
        slices = [RainNowcastSlice.model_validate(item) for item in payload] if isinstance(payload, list) else []
        return RainNowcastBlock(
            status=status.status,
            slices=slices,
            updated_at=status.updated_at,
            source_model=PIAF_SOURCE_MODEL if slices else None,
        ), status

    @staticmethod
    def _slices(piaf: PiafRun, lat: float, lon: float) -> list[RainNowcastSlice]:
        values = piaf.point(lat, lon)
        if values is None:
            raise PiafOutsideError(f"({lat}, {lon}) hors de la carte PIAF")
        slices: list[RainNowcastSlice] = []
        for k, raw in enumerate(values, start=1):
            mm = piaf_mm_5min(raw)
            if mm is None:
                raise ValueError(f"valeur PIAF illisible pour le run {piaf_coverage_id(piaf.run)}")
            slices.append(rain_slice_from_mm_5min(piaf.run + timedelta(seconds=PIAF_STEP_SECONDS * k), mm))
        return slices

    async def _current_run(self, headers: dict[str, str], now: datetime) -> PiafRun:
        """La carte à servir : celle en mémoire si elle est récente, sinon celle du dernier run publié.

        Une carte de moins de `piaf_ttl_seconds` (depuis son chargement) est servie telle quelle. Plus vieille mais de
        moins de `piaf_grid_max_stale_seconds`, elle est servie pendant qu'un run plus récent se télécharge en tâche
        de fond. Sans carte, ou trop vieille, la requête attend le téléchargement.
        """
        current = self._run
        if current is not None:
            age = time.time() - current.loaded_at
            if age < self._settings.piaf_ttl_seconds:
                return current
            if age < self._settings.piaf_grid_max_stale_seconds:
                if self._refresh is None or self._refresh.done():
                    self._refresh = asyncio.create_task(self._refresh_quietly(headers, now))
                return current
        return await self._load(headers, now, seen=current)

    async def _refresh_quietly(self, headers: dict[str, str], now: datetime) -> None:
        try:
            await self._load(headers, now, seen=self._run)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("rafraîchissement PIAF en échec : %s", redact_secret(str(exc), self._settings))

    async def _load(self, headers: dict[str, str], now: datetime, seen: PiafRun | None) -> PiafRun:
        """Télécharge le dernier run publié (une requête par échéance), une seule fois à la fois."""
        async with self._lock:
            if self._run is not seen and self._run is not None:
                return self._run  # chargé par une autre requête pendant l'attente du verrou
            run, first = await self._first_step(headers, now)
            if self._run is not None and self._run.run == run:
                self._run.loaded_at = time.time()  # pas de run plus récent publié : on regardera au prochain délai
                return self._run
            sem = asyncio.Semaphore(self._settings.piaf_concurrency)

            async def step(seconds: int) -> LatLonGrid:
                async with sem:
                    resp = await self._get_step(run, seconds, headers)
                    resp.raise_for_status()
                    return decode_latlon_simple(resp.content)

            seconds = [PIAF_STEP_SECONDS * i for i in range(2, self._settings.piaf_steps + 1)]
            rest = await asyncio.gather(*(step(s) for s in seconds))
            grids = [first, *rest]
            shape = first.values.shape
            if any(g.values.shape != shape or g.lat0 != first.lat0 or g.lon0 != first.lon0 for g in grids):
                raise ValueError(f"cartes PIAF de géométries différentes dans le run {piaf_coverage_id(run)}")
            self._run = PiafRun(run=run, steps=grids)
            return self._run

    async def _first_step(self, headers: dict[str, str], now: datetime) -> tuple[datetime, LatLonGrid]:
        """Le dernier run publié et sa première échéance (sans le catalogue de 2 Mo)."""
        for candidate in piaf_run_candidates(now, self._settings.piaf_publish_delay_minutes):
            if self._run is not None and candidate <= self._run.run:
                return self._run.run, self._run.steps[0]  # rien de plus récent que la carte en mémoire
            resp = await self._get_step(candidate, PIAF_STEP_SECONDS, headers)
            if resp.status_code in (400, 404):
                continue  # run pas encore publié (ou déjà retiré) : essayer le précédent
            resp.raise_for_status()
            return candidate, decode_latlon_simple(resp.content)
        raise ValueError("aucun run PIAF publié parmi les candidats")

    async def _get_step(self, run: datetime, step_seconds: int, headers: dict[str, str]) -> httpx.Response:
        params = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", piaf_coverage_id(run)),
            ("subset", f"time({step_seconds})"),
            ("format", PIAF_FORMAT),
        ]
        req_headers = {"User-Agent": self._settings.user_agent, **headers}
        return await self._client.get(
            self._settings.meteofrance_piaf_url,
            params=params,
            headers=req_headers,
            timeout=self._settings.http_timeout_seconds,
        )
