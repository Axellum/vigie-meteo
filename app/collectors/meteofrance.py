"""Collecteur officiel Météo-France : pluie dans l'heure PIAF.

Les vigilances et les observations 6 min sont lues par Vigie elle-même (vigie-api) : elles ne passent plus par
cette passerelle (retirées le 16/11/2026).

Protégé par cache SQLite WAL + Circuit Breaker pour absorber les erreurs 429/502
du portail officiel Météo-France sans jamais faire échouer l'API cliente.
"""

import asyncio
import math
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.collectors.base import ResilientCollector, snap_coord
from app.config import redact_secret
from app.models import (
    RainNowcastBlock,
    RainNowcastSlice,
    SourceStatus,
)


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
PIAF_STEP_SECONDS = 300
_PIAF_COVERAGE = "TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE___{run}_PT5M"
_PIAF_TUPLE_RE = re.compile(r"<gml:tupleList>\s*([^<]*?)\s*</gml:tupleList>")


def piaf_run_candidates(now: datetime, publish_delay_minutes: int, count: int = 3) -> list[datetime]:
    """Runs PIAF à essayer, du plus récent au plus ancien (un run toutes les 5 min, publié en retard)."""
    ref = now.astimezone(UTC) - timedelta(minutes=publish_delay_minutes)
    ref = ref.replace(minute=ref.minute - ref.minute % 5, second=0, microsecond=0)
    return [ref - timedelta(minutes=5 * i) for i in range(count)]


def piaf_coverage_id(run: datetime) -> str:
    """Identifiant WCS d'un run PIAF (ex: `…___2026-11-16T20.25.00Z_PT5M`)."""
    return _PIAF_COVERAGE.format(run=run.astimezone(UTC).strftime("%Y-%m-%dT%H.%M.%SZ"))


def parse_piaf_point(text: str) -> float | None:
    """Lit la valeur d'un point PIAF (GML `tupleList`) : cumul en mm sur 5 min, ou None si absente/invalide."""
    match = _PIAF_TUPLE_RE.search(text)
    if match is None:
        return None
    parts = match.group(1).split()
    if not parts:
        return None
    value = _opt_float(parts[0])
    if value is None or math.isnan(value) or value < 0.0 or value > 500.0:
        return None
    return value


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


class MeteoFranceNowcastCollector(_MeteoFranceAuthCollector):
    """Pluie dans l'heure officielle : PIAF, pas de 5 min (une requête WCS par échéance).

    Le résultat d'un point est mis en cache `piaf_ttl_seconds` (un run toutes les 5 min) :
    ~13 requêtes par point et par run, sous le quota de 50/min de l'abonnement.
    """

    provider_name = "meteofrance-nowcast"

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
            slices = await self._fetch_piaf(s_lat, s_lon, headers, now or datetime.now(tz=UTC))
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

    async def _get_point(
        self,
        run: datetime,
        step_seconds: int,
        lat: float,
        lon: float,
        headers: dict[str, str],
    ) -> httpx.Response:
        params = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", piaf_coverage_id(run)),
            ("subset", f"time({step_seconds})"),
            ("subset", f"lat({lat})"),
            ("subset", f"long({lon})"),
            ("format", "image/tiff"),
        ]
        req_headers = {"User-Agent": self._settings.user_agent, **headers}
        return await self._client.get(
            self._settings.meteofrance_piaf_url,
            params=params,
            headers=req_headers,
            timeout=self._settings.http_timeout_seconds,
        )

    async def _fetch_piaf(
        self,
        lat: float,
        lon: float,
        headers: dict[str, str],
        now: datetime,
    ) -> list[RainNowcastSlice]:
        """Trouve le dernier run publié (sans le catalogue de 2 Mo), puis lit chaque échéance."""
        run: datetime | None = None
        first: float | None = None
        for candidate in piaf_run_candidates(now, self._settings.piaf_publish_delay_minutes):
            resp = await self._get_point(candidate, PIAF_STEP_SECONDS, lat, lon, headers)
            if resp.status_code in (400, 404):
                continue  # run pas encore publié (ou déjà retiré) : essayer le précédent
            resp.raise_for_status()
            first = parse_piaf_point(resp.text)
            run = candidate
            break
        if run is None:
            raise ValueError("aucun run PIAF publié parmi les candidats")

        sem = asyncio.Semaphore(self._settings.piaf_concurrency)

        async def read_step(step_seconds: int) -> float | None:
            async with sem:
                resp = await self._get_point(run, step_seconds, lat, lon, headers)
                resp.raise_for_status()
                return parse_piaf_point(resp.text)

        steps = [PIAF_STEP_SECONDS * i for i in range(2, self._settings.piaf_steps + 1)]
        rest = await asyncio.gather(*(read_step(s) for s in steps))
        values: list[float] = []
        for raw in (first, *rest):
            if raw is None:
                raise ValueError(f"valeur PIAF illisible pour le run {piaf_coverage_id(run)}")
            values.append(raw)

        return [
            rain_slice_from_mm_5min(run + timedelta(seconds=PIAF_STEP_SECONDS * i), v)
            for i, v in enumerate(values, start=1)
        ]
