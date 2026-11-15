"""Collecteur Open-Meteo (modèles Météo-France AROME HD + ARPEGE Europe).

Interroge en priorité l'instance Open-Meteo locale sur Oracle (`http://open-meteo:8080`),
avec bascule automatique sur l'URL de secours si configurée.
Fusionne AROME HD (~1,5 km, 0-48 h) et ARPEGE Europe (~11 km, J+2 à J+4) en une seule série.
"""

from datetime import UTC, datetime
from typing import Any

from app.collectors.base import ResilientCollector, snap_coord
from app.models import (
    CurrentWeather,
    DailyForecastPoint,
    HourlyForecastPoint,
    RainNowcastBlock,
    RainNowcastSlice,
    SourceStatus,
)

AROME_HD_MODEL = "meteofrance_arome_france_hd"
AROME_HD_15M_MODEL = "meteofrance_arome_france_hd_15min"
ARPEGE_EUROPE_MODEL = "meteofrance_arpege_europe"

HOURLY_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m,"
    "wind_direction_10m,cloud_cover,surface_pressure,uv_index,shortwave_radiation,weather_code"
)
CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "wind_speed_10m,wind_gusts_10m,wind_direction_10m,surface_pressure,cloud_cover,weather_code,is_day"
)
DAILY_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "wind_speed_10m_max,wind_gusts_10m_max,uv_index_max,sunrise,sunset"
)


def _parse_iso_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    # Open-Meteo retourne parfois "2026-11-15T12:00" sans fuseau lorsqu'un timezone est précisé
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _pick_series_value(
    hourly_dict: dict[str, list[Any]],
    field: str,
    idx: int,
    primary_suffix: str,
    fallback_suffix: str,
) -> tuple[Any, str | None]:
    """Choisit la valeur AROME HD si non nulle, sinon ARPEGE Europe."""
    primary_key = f"{field}_{primary_suffix}"
    fallback_key = f"{field}_{fallback_suffix}"

    primary_list = hourly_dict.get(primary_key)
    if primary_list and idx < len(primary_list) and primary_list[idx] is not None:
        return primary_list[idx], primary_suffix

    fallback_list = hourly_dict.get(fallback_key)
    if fallback_list and idx < len(fallback_list) and fallback_list[idx] is not None:
        return fallback_list[idx], fallback_suffix

    # Cas où Open-Meteo n'a retourné qu'un seul modèle (sans suffixe)
    plain_list = hourly_dict.get(field)
    if plain_list and idx < len(plain_list) and plain_list[idx] is not None:
        return plain_list[idx], primary_suffix

    return None, None


class OpenMeteoCollector(ResilientCollector):
    """Client résilient pour l'API Open-Meteo locale/distante."""

    provider_name = "open-meteo"

    async def _fetch_meteofrance(
        self,
        cache_key: str,
        ttl_seconds: int,
        params: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, SourceStatus]:
        """Interroge `/v1/meteofrance` sur l'instance primaire avec repli configurable."""
        primary_url = f"{self._settings.open_meteo_base_url.rstrip('/')}/v1/meteofrance"
        payload, status = await self.fetch_with_cache(
            cache_key=cache_key,
            url=primary_url,
            ttl_seconds=ttl_seconds,
            params=params,
        )

        # Repli vers l'API publique Open-Meteo si l'instance locale n'est pas joignable
        if payload is None and self._settings.open_meteo_fallback_url:
            fallback_url = f"{self._settings.open_meteo_fallback_url.rstrip('/')}/v1/meteofrance"
            if fallback_url != primary_url:
                payload, status = await self.fetch_with_cache(
                    cache_key=cache_key,
                    url=fallback_url,
                    ttl_seconds=ttl_seconds,
                    params=params,
                )

        return payload, status

    async def get_france_forecast(
        self,
        lat: float,
        lon: float,
        days: int = 4,
    ) -> tuple[dict[str, Any] | None, SourceStatus, float, float]:
        """Récupère les données brutes Open-Meteo (AROME HD + ARPEGE) sur grille snappée."""
        s_lat = snap_coord(lat)
        s_lon = snap_coord(lon)
        cache_key = f"om:fr:{s_lat}:{s_lon}:{days}"
        params = {
            "latitude": s_lat,
            "longitude": s_lon,
            "models": f"{AROME_HD_MODEL},{ARPEGE_EUROPE_MODEL}",
            "current": CURRENT_VARS,
            "hourly": HOURLY_VARS,
            "daily": DAILY_VARS,
            "forecast_days": days,
            "timezone": "UTC",
        }
        payload, status = await self._fetch_meteofrance(cache_key, self._settings.forecast_ttl_seconds, params)
        return payload, status, s_lat, s_lon

    async def get_rain_nowcast_15m(
        self,
        lat: float,
        lon: float,
    ) -> tuple[RainNowcastBlock, SourceStatus]:
        """Récupère les précipitations 15 min AROME HD en repli du nowcast pluie officiel."""
        s_lat = snap_coord(lat, step=0.01)
        s_lon = snap_coord(lon, step=0.01)
        cache_key = f"om:rain15m:{s_lat}:{s_lon}"
        params = {
            "latitude": s_lat,
            "longitude": s_lon,
            "models": AROME_HD_15M_MODEL,
            "minutely_15": "precipitation",
            "forecast_minutely_15": 8,
            "timezone": "UTC",
        }
        payload, status = await self._fetch_meteofrance(cache_key, self._settings.nowcast_ttl_seconds, params)
        if payload is None:
            return RainNowcastBlock(
                status=status.status,
                slices=[],
                updated_at=status.updated_at,
                source_model=AROME_HD_15M_MODEL,
            ), status

        slices = self.parse_rain_nowcast_15m(payload)
        return RainNowcastBlock(
            status=status.status,
            slices=slices,
            updated_at=status.updated_at,
            source_model=AROME_HD_15M_MODEL,
        ), status

    @staticmethod
    def parse_rain_nowcast_15m(payload: dict[str, Any]) -> list[RainNowcastSlice]:
        """Convertit les tranches `minutely_15.precipitation` (mm/15 min) en `RainNowcastSlice`."""
        m15 = payload.get("minutely_15")
        if not isinstance(m15, dict):
            return []
        times = m15.get("time") or []
        values = m15.get(f"precipitation_{AROME_HD_15M_MODEL}") or m15.get("precipitation") or []
        slices: list[RainNowcastSlice] = []
        for idx, raw_time in enumerate(times):
            dt = _parse_iso_dt(raw_time)
            if dt is None:
                continue
            raw_val = values[idx] if idx < len(values) else None
            if raw_val is None:
                slices.append(RainNowcastSlice(time=dt))
                continue
            precip_15m = float(raw_val)
            mm_h = round(precip_15m * 4.0, 2)
            if precip_15m < 0.05:
                level, label = 1, "Temps sec"
            elif precip_15m < 0.4:
                level, label = 2, "Pluie faible"
            elif precip_15m < 1.5:
                level, label = 3, "Pluie modérée"
            else:
                level, label = 4, "Pluie forte"
            slices.append(RainNowcastSlice(time=dt, rain_level=level, rain_label=label, rain_mm_h=mm_h))
        return slices

    async def proxy_raw_forecast(self, query_params: dict[str, Any]) -> tuple[dict[str, Any] | None, SourceStatus]:
        """Proxy transparent compatible `GET /v1/forecast` d'Open-Meteo (routé sur `/v1/meteofrance` par défaut)."""
        sorted_items = sorted((k, str(v)) for k, v in query_params.items())
        cache_key = "om:raw:" + "&".join(f"{k}={v}" for k, v in sorted_items)
        endpoint = "/v1/forecast" if "models" in query_params else "/v1/meteofrance"
        url = f"{self._settings.open_meteo_base_url.rstrip('/')}{endpoint}"
        return await self.fetch_with_cache(
            cache_key=cache_key,
            url=url,
            ttl_seconds=self._settings.forecast_ttl_seconds,
            params=query_params,
        )

    async def proxy_raw_marine(self, query_params: dict[str, Any]) -> tuple[dict[str, Any] | None, SourceStatus]:
        """Proxy transparent compatible `GET /v1/marine` d'Open-Meteo (MFWAM vagues, courants et SST)."""
        sorted_items = sorted((k, str(v)) for k, v in query_params.items())
        cache_key = "om:marine:" + "&".join(f"{k}={v}" for k, v in sorted_items)
        url = f"{self._settings.open_meteo_base_url.rstrip('/')}/v1/marine"
        return await self.fetch_with_cache(
            cache_key=cache_key,
            url=url,
            ttl_seconds=self._settings.forecast_ttl_seconds,
            params=query_params,
        )

    @staticmethod
    def parse_current(payload: dict[str, Any]) -> CurrentWeather | None:
        """Extrait les conditions actuelles depuis la réponse Open-Meteo."""
        current = payload.get("current")
        if not isinstance(current, dict) or not current.get("time"):
            return None
        dt = _parse_iso_dt(current.get("time"))
        if dt is None:
            return None

        def _val(key: str) -> Any:
            return current.get(f"{key}_{AROME_HD_MODEL}", current.get(f"{key}_{ARPEGE_EUROPE_MODEL}", current.get(key)))

        is_day_raw = _val("is_day")
        return CurrentWeather(
            time=dt,
            temperature_c=_val("temperature_2m"),
            apparent_temperature_c=_val("apparent_temperature"),
            relative_humidity_pct=_val("relative_humidity_2m"),
            precipitation_mm=_val("precipitation"),
            wind_speed_kmh=_val("wind_speed_10m"),
            wind_gusts_kmh=_val("wind_gusts_10m"),
            wind_direction_deg=_val("wind_direction_10m"),
            pressure_hpa=_val("surface_pressure"),
            cloud_cover_pct=_val("cloud_cover"),
            weather_code=_val("weather_code"),
            is_day=bool(is_day_raw) if is_day_raw is not None else None,
            source_model=AROME_HD_MODEL if f"temperature_2m_{AROME_HD_MODEL}" in current else ARPEGE_EUROPE_MODEL,
        )

    @staticmethod
    def parse_hourly(payload: dict[str, Any]) -> list[HourlyForecastPoint]:
        """Fusionne les séries horaires AROME HD (0-48h) et ARPEGE Europe (suite)."""
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            return []
        times = hourly.get("time") or []
        points: list[HourlyForecastPoint] = []

        for idx, raw_time in enumerate(times):
            dt = _parse_iso_dt(raw_time)
            if dt is None:
                continue

            temp, temp_model = _pick_series_value(hourly, "temperature_2m", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            app_temp, _ = _pick_series_value(hourly, "apparent_temperature", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            hum, _ = _pick_series_value(hourly, "relative_humidity_2m", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            precip, _ = _pick_series_value(hourly, "precipitation", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            precip_prob, _ = _pick_series_value(
                hourly, "precipitation_probability", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL
            )
            wind, _ = _pick_series_value(hourly, "wind_speed_10m", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            gusts, _ = _pick_series_value(hourly, "wind_gusts_10m", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            wind_dir, _ = _pick_series_value(hourly, "wind_direction_10m", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            clouds, _ = _pick_series_value(hourly, "cloud_cover", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            pressure, _ = _pick_series_value(hourly, "surface_pressure", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            uv, _ = _pick_series_value(hourly, "uv_index", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            rad, _ = _pick_series_value(hourly, "shortwave_radiation", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)
            wmo, _ = _pick_series_value(hourly, "weather_code", idx, AROME_HD_MODEL, ARPEGE_EUROPE_MODEL)

            points.append(
                HourlyForecastPoint(
                    time=dt,
                    temperature_c=temp,
                    apparent_temperature_c=app_temp,
                    relative_humidity_pct=hum,
                    precipitation_mm=precip,
                    precipitation_probability_pct=precip_prob,
                    wind_speed_kmh=wind,
                    wind_gusts_kmh=gusts,
                    wind_direction_deg=wind_dir,
                    cloud_cover_pct=clouds,
                    pressure_hpa=pressure,
                    uv_index=uv,
                    shortwave_radiation_wm2=rad,
                    weather_code=wmo,
                    source_model=temp_model or ARPEGE_EUROPE_MODEL,
                )
            )
        return points

    @staticmethod
    def parse_daily(payload: dict[str, Any]) -> list[DailyForecastPoint]:
        """Extrait les prévisions journalières agrégées."""
        daily = payload.get("daily")
        if not isinstance(daily, dict):
            return []
        dates = daily.get("time") or []
        points: list[DailyForecastPoint] = []

        for idx, date_str in enumerate(dates):
            t_min, _ = _pick_series_value(daily, "temperature_2m_min", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            t_max, _ = _pick_series_value(daily, "temperature_2m_max", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            p_sum, _ = _pick_series_value(daily, "precipitation_sum", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            w_max, _ = _pick_series_value(daily, "wind_speed_10m_max", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            g_max, _ = _pick_series_value(daily, "wind_gusts_10m_max", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            uv_max, _ = _pick_series_value(daily, "uv_index_max", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            wmo, _ = _pick_series_value(daily, "weather_code", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            sr_raw, _ = _pick_series_value(daily, "sunrise", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)
            ss_raw, _ = _pick_series_value(daily, "sunset", idx, ARPEGE_EUROPE_MODEL, AROME_HD_MODEL)

            points.append(
                DailyForecastPoint(
                    date=str(date_str),
                    temperature_min_c=t_min,
                    temperature_max_c=t_max,
                    precipitation_sum_mm=p_sum,
                    wind_speed_max_kmh=w_max,
                    wind_gusts_max_kmh=g_max,
                    uv_index_max=uv_max,
                    sunrise=_parse_iso_dt(sr_raw),
                    sunset=_parse_iso_dt(ss_raw),
                    weather_code=wmo,
                )
            )
        return points
