"""Collecteur officiel Météo-France (Vigilances départementales, pluie dans l'heure PIAF, observations).

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
    ObservationStation,
    RainNowcastBlock,
    RainNowcastSlice,
    SourceStatus,
    VigilanceBlock,
    VigilanceColor,
    VigilanceDepartment,
    VigilancePhenomenon,
)

COLOR_MAP: dict[int, VigilanceColor] = {
    1: "green",
    2: "yellow",
    3: "orange",
    4: "red",
}

# Table officielle des phénomènes de vigilance Météo-France
PHENOMENON_NAMES: dict[int, str] = {
    1: "Vent",
    2: "Pluie-inondation",
    3: "Orages",
    4: "Crues",
    5: "Neige-verglas",
    6: "Canicule",
    7: "Grand froid",
    8: "Avalanches",
    9: "Vagues-submersion",
}


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


class MeteoFranceVigilanceCollector(_MeteoFranceAuthCollector):
    """Collecteur des vigilances officielles Météo-France (DPVigilance v1)."""

    provider_name = "meteofrance-vigilance"

    async def get_all_vigilances(self) -> tuple[dict[str, Any] | None, SourceStatus]:
        """Récupère la carte de vigilance France entière (avec cache global)."""
        headers = self._auth_headers()
        if headers is None:
            return None, SourceStatus(status="not_configured", provider=self.provider_name)

        return await self.fetch_with_cache(
            cache_key="mf:vigilance:france",
            url=self._settings.meteofrance_vigilance_url,
            ttl_seconds=self._settings.vigilance_ttl_seconds,
            headers=headers,
        )

    async def get_department_vigilance(self, dept: str) -> tuple[VigilanceBlock, SourceStatus]:
        """Retourne la vigilance d'un département donné (ex: '75', '40', '2A')."""
        payload, status = await self.get_all_vigilances()
        if payload is None:
            return VigilanceBlock(status=status.status, department=None), status

        parsed = self.parse_department(payload, dept)
        return VigilanceBlock(status=status.status, department=parsed), status

    @staticmethod
    def parse_all_departments(payload: dict[str, Any]) -> list[VigilanceDepartment]:
        """Parse tous les départements présents dans la réponse DPVigilance."""
        product = payload.get("product", payload)
        periods = product.get("periods") or []
        if not periods:
            return []

        # On prend la période courante (généralement J = index 0)
        current_period = periods[0]
        timelaps = current_period.get("timelaps") or {}
        domain_ids = timelaps.get("domain_ids") or []
        issued_at = _parse_dt(product.get("update_time"))
        valid_until = _parse_dt(current_period.get("end_validity_time"))

        results: list[VigilanceDepartment] = []
        for dom in domain_ids:
            dept_id = str(dom.get("domain_id", "")).strip().upper()
            if not dept_id or dept_id == "FRA":
                continue
            max_color_id = int(dom.get("max_color_id") or 1)
            color: VigilanceColor = COLOR_MAP.get(max_color_id, "green")

            phenomena: list[VigilancePhenomenon] = []
            for item in dom.get("phenomenon_items") or []:
                ph_id = int(item.get("phenomenon_id") or 0)
                ph_color_id = int(item.get("phenomenon_max_color_id") or 1)
                if ph_color_id <= 1:
                    continue
                ph_name = item.get("phenomenon_label") or PHENOMENON_NAMES.get(ph_id, f"Phénomène {ph_id}")
                timelaps_items = item.get("timelaps_items") or []
                start_t = _parse_dt(timelaps_items[0].get("begin_time")) if timelaps_items else None
                end_t = _parse_dt(timelaps_items[-1].get("end_time")) if timelaps_items else None
                phenomena.append(
                    VigilancePhenomenon(
                        id=ph_id,
                        name=str(ph_name),
                        color=COLOR_MAP.get(ph_color_id, "yellow"),
                        level=ph_color_id,
                        start_time=start_t,
                        end_time=end_t,
                    )
                )

            results.append(
                VigilanceDepartment(
                    department=dept_id,
                    color=color,
                    level=max_color_id,
                    phenomena=phenomena,
                    issued_at=issued_at,
                    valid_until=valid_until,
                )
            )
        return results

    @classmethod
    def parse_department(cls, payload: dict[str, Any], dept: str) -> VigilanceDepartment | None:
        """Extrait un département spécifique de la réponse DPVigilance."""
        target = dept.strip().upper().zfill(2) if dept.strip().isdigit() else dept.strip().upper()
        for entry in cls.parse_all_departments(payload):
            if entry.department == target:
                return entry
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


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance grand cercle (km) entre deux points WGS84."""
    r_km = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r_km * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def latest_6min_utc(now: datetime | None = None) -> datetime:
    """Calcule la dernière échéance 6 min publiable (marge 6 min, multiple exact de 6 min)."""
    ref = (now or datetime.now(tz=UTC)).astimezone(UTC) - timedelta(minutes=6)
    floored_min = (ref.minute // 6) * 6
    return ref.replace(minute=floored_min, second=0, microsecond=0)


def _opt_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_int(val: Any) -> int | None:
    num = _opt_float(val)
    return int(round(num)) if num is not None else None


class MeteoFranceObservationsCollector(_MeteoFranceAuthCollector):
    """Collecteur temps réel 6 minutes des stations sol via `DPPaquetObs v2`."""

    provider_name = "meteofrance-obs"

    async def get_all_stations_6m(
        self,
        dt_utc: datetime | None = None,
    ) -> tuple[list[dict[str, Any]] | None, SourceStatus]:
        """Télécharge le paquet GeoJSON national 6 minutes (1 appel partagé pour toutes les positions)."""
        headers = self._auth_headers()
        if headers is None:
            return None, SourceStatus(status="not_configured", provider=self.provider_name)

        target_dt = (dt_utc or latest_6min_utc()).astimezone(UTC)
        floored_min = (target_dt.minute // 6) * 6
        target_dt = target_dt.replace(minute=floored_min, second=0, microsecond=0)
        date_str = target_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        payload, status = await self.fetch_with_cache(
            cache_key=f"mf:obs6m:{date_str}",
            url=self._settings.meteofrance_paquet_obs_url,
            ttl_seconds=self._settings.obs_ttl_seconds,
            params={"date": date_str, "format": "geojson"},
            headers=headers,
        )
        if payload is None:
            return None, status

        features = self.extract_features(payload)
        return features, status

    async def get_nearest_observation(
        self,
        lat: float,
        lon: float,
        max_radius_km: float = 50.0,
        dt_utc: datetime | None = None,
    ) -> tuple[ObservationStation | None, SourceStatus]:
        """Retourne l'observation 6 min de la station la plus proche dans `max_radius_km`."""
        features, status = await self.get_all_stations_6m(dt_utc=dt_utc)
        if features is None:
            return None, status
        station = self.parse_nearest_station(features, lat=lat, lon=lon, max_radius_km=max_radius_km)
        return station, status

    @staticmethod
    def extract_features(payload: Any) -> list[dict[str, Any]]:
        """Accepte une liste GeoJSON brute (format natif DPPaquetObs v2) ou une FeatureCollection."""
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            feats = payload.get("features")
            if isinstance(feats, list):
                return [item for item in feats if isinstance(item, dict)]
        return []

    @classmethod
    def parse_nearest_station(
        cls,
        features: list[dict[str, Any]],
        lat: float,
        lon: float,
        max_radius_km: float = 50.0,
    ) -> ObservationStation | None:
        """Sélectionne la station valide la plus proche et convertit les grandeurs en unités SI standard."""
        best: tuple[float, dict[str, Any], float, float, datetime] | None = None
        for feat in features:
            geom = feat.get("geometry")
            props = feat.get("properties")
            if not isinstance(geom, dict) or not isinstance(props, dict):
                continue
            coords = geom.get("coordinates")
            if not isinstance(coords, (list, tuple)) or len(coords) < 2:
                continue
            s_lon = _opt_float(coords[0])
            s_lat = _opt_float(coords[1])
            if s_lat is None or s_lon is None:
                continue
            station_id = str(props.get("geo_id_insee") or "").strip()
            obs_dt = _parse_dt(props.get("validity_time"))
            if not station_id or obs_dt is None:
                continue

            dist_km = _haversine_km(lat, lon, s_lat, s_lon)
            if dist_km > max_radius_km:
                continue
            if best is None or dist_km < best[0]:
                best = (dist_km, props, s_lat, s_lon, obs_dt)

        if best is None:
            return None
        dist_km, props, s_lat, s_lon, obs_dt = best

        t_k = _opt_float(props.get("t"))
        td_k = _opt_float(props.get("td"))
        ff_ms = _opt_float(props.get("ff"))
        fxi10_ms = _opt_float(props.get("fxi10"))
        pmer_pa = _opt_float(props.get("pmer"))
        rr_per = _opt_float(props.get("rr_per"))

        return ObservationStation(
            station_id=station_id_str(props),
            latitude=s_lat,
            longitude=s_lon,
            distance_km=round(dist_km, 2),
            observed_at=obs_dt,
            temperature_c=round(t_k - 273.15, 2) if t_k is not None else None,
            dew_point_c=round(td_k - 273.15, 2) if td_k is not None else None,
            relative_humidity_pct=_opt_int(props.get("u")),
            wind_speed_kmh=round(ff_ms * 3.6, 1) if ff_ms is not None else None,
            wind_direction_deg=_opt_int(props.get("dd")),
            wind_gusts_kmh=round(fxi10_ms * 3.6, 1) if fxi10_ms is not None else None,
            wind_gusts_direction_deg=_opt_int(props.get("dxi10")),
            precipitation_6m_mm=round(rr_per, 2) if rr_per is not None else None,
            pressure_msl_hpa=round(pmer_pa / 100.0, 2) if pmer_pa is not None else None,
            visibility_m=_opt_int(props.get("vv")),
        )


def station_id_str(props: dict[str, Any]) -> str:
    return str(props.get("geo_id_insee", "")).strip()
