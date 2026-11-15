"""Collecteur officiel Météo-France (Vigilances départementales & Pluie dans l'heure).

Protégé par cache SQLite WAL + Circuit Breaker pour absorber les erreurs 429/502
du portail officiel Météo-France sans jamais faire échouer l'API cliente.
"""

from datetime import UTC, datetime
from typing import Any

from app.collectors.base import ResilientCollector, snap_coord
from app.models import (
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


class MeteoFranceVigilanceCollector(ResilientCollector):
    """Collecteur des vigilances officielles Météo-France (DPVigilance v1)."""

    provider_name = "meteofrance-vigilance"

    def _auth_headers(self) -> dict[str, str] | None:
        if self._settings.meteofrance_api_key is None:
            return None
        token = self._settings.meteofrance_api_key.get_secret_value().strip()
        if not token:
            return None
        # WSO2 (Météo-France) rejette avec 401 si Authorization: Bearer est envoyé avec un token apiKey
        return {"apikey": token, "accept": "*/*"}

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


class MeteoFranceNowcastCollector(ResilientCollector):
    """Collecteur de la pluie dans l'heure Météo-France (pas de 5-10 min)."""

    provider_name = "meteofrance-nowcast"

    def _auth_headers(self) -> dict[str, str] | None:
        if self._settings.meteofrance_api_key is None:
            return None
        token = self._settings.meteofrance_api_key.get_secret_value().strip()
        if not token:
            return None
        # WSO2 (Météo-France) rejette avec 401 si Authorization: Bearer est envoyé avec un token apiKey
        return {"apikey": token, "accept": "*/*"}

    async def get_rain_nowcast(self, lat: float, lon: float) -> tuple[RainNowcastBlock, SourceStatus]:
        """Récupère les tranches de pluie dans l'heure pour un point GPS snappé."""
        headers = self._auth_headers()
        if headers is None:
            status = SourceStatus(status="not_configured", provider=self.provider_name)
            return RainNowcastBlock(status="not_configured", slices=[]), status

        s_lat = snap_coord(lat, step=0.01)
        s_lon = snap_coord(lon, step=0.01)
        payload, status = await self.fetch_with_cache(
            cache_key=f"mf:nowcast:{s_lat}:{s_lon}",
            url=self._settings.meteofrance_nowcast_url,
            ttl_seconds=self._settings.nowcast_ttl_seconds,
            params={"lat": s_lat, "lon": s_lon},
            headers=headers,
        )
        if payload is None:
            return RainNowcastBlock(status=status.status, slices=[], updated_at=status.updated_at), status

        slices = self.parse_nowcast(payload)
        return RainNowcastBlock(status=status.status, slices=slices, updated_at=status.updated_at), status

    @staticmethod
    def parse_nowcast(payload: dict[str, Any]) -> list[RainNowcastSlice]:
        """Parse la liste des tranches 5-10 min retournée par Météo-France."""
        raw_list = payload.get("properties", payload).get("forecast") or payload.get("forecast") or []
        slices: list[RainNowcastSlice] = []
        for item in raw_list:
            dt = _parse_dt(item.get("time") or item.get("dt"))
            if dt is None:
                continue
            rain_level = item.get("rain_intensity") or item.get("rain")
            slices.append(
                RainNowcastSlice(
                    time=dt,
                    rain_level=int(rain_level) if rain_level is not None else None,
                    rain_label=item.get("rain_intensity_description") or item.get("desc"),
                    rain_mm_h=item.get("rain_mm_h"),
                )
            )
        return slices
