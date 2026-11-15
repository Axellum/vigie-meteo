"""Service d'agrégation météo unifiant Open-Meteo (AROME HD + ARPEGE) et Météo-France."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from app.collectors.meteofrance import (
    MeteoFranceNowcastCollector,
    MeteoFranceObservationsCollector,
    MeteoFranceVigilanceCollector,
)
from app.collectors.open_meteo import OpenMeteoCollector
from app.models import (
    CompactWeatherResponse,
    ObservationsResponse,
    ObservationStation,
    RainNowcastBlock,
    SourceStatus,
    UnifiedWeatherResponse,
    VigilanceBlock,
)


class WeatherAggregatorService:
    """Orchestre en parallèle les collecteurs pour produire une réponse unifiée et résiliente."""

    def __init__(
        self,
        open_meteo: OpenMeteoCollector,
        vigilance: MeteoFranceVigilanceCollector,
        nowcast: MeteoFranceNowcastCollector,
        observations: MeteoFranceObservationsCollector | None = None,
    ) -> None:
        self._om = open_meteo
        self._vig = vigilance
        self._now = nowcast
        self._obs = observations

    async def _resolve_rain_nowcast(self, lat: float, lon: float) -> tuple[RainNowcastBlock, SourceStatus]:
        """Retourne le nowcast officiel Météo-France, ou bascule sur AROME HD 15 min si indisponible."""
        block, status = await self._now.get_rain_nowcast(lat=lat, lon=lon)
        if block.status in ("fresh", "stale") and block.slices:
            return block, status

        fallback_block, fallback_status = await self._om.get_rain_nowcast_15m(lat=lat, lon=lon)
        if fallback_block.slices:
            return fallback_block, fallback_status
        return block, status

    async def get_unified_weather(
        self,
        lat: float,
        lon: float,
        dept: str | None = None,
        days: int = 4,
        include_obs: bool = False,
        obs_radius_km: float = 50.0,
    ) -> UnifiedWeatherResponse:
        """Agrège prévisions, pluie dans l'heure, vigilances et observation sol optionnelle."""
        om_task = self._om.get_france_forecast(lat=lat, lon=lon, days=days)
        now_task = self._resolve_rain_nowcast(lat=lat, lon=lon)
        vig_task = self._vig.get_department_vigilance(dept) if dept else None
        obs_task = (
            self._obs.get_nearest_observation(lat=lat, lon=lon, max_radius_km=obs_radius_km)
            if include_obs and self._obs is not None
            else None
        )

        coros: list[Any] = [om_task, now_task]
        if vig_task is not None:
            coros.append(vig_task)
        if obs_task is not None:
            coros.append(obs_task)

        gathered = await asyncio.gather(*coros)
        om_payload, om_status, s_lat, s_lon = gathered[0]
        now_block, now_status = gathered[1]

        idx = 2
        if vig_task is not None:
            vig_block, vig_status = gathered[idx]
            idx += 1
        else:
            vig_status = SourceStatus(status="not_configured", provider=self._vig.provider_name)
            vig_block = VigilanceBlock(status="not_configured", department=None)

        obs_station: ObservationStation | None = None
        obs_status: SourceStatus | None = None
        if obs_task is not None:
            obs_station, obs_status = gathered[idx]

        current = self._om.parse_current(om_payload) if om_payload else None
        hourly = self._om.parse_hourly(om_payload) if om_payload else []
        daily = self._om.parse_daily(om_payload) if om_payload else []

        sources: dict[str, SourceStatus] = {
            "forecast": om_status,
            "rain_nowcast": now_status,
            "vigilance": vig_status,
        }
        if obs_status is not None:
            sources["observation"] = obs_status

        return UnifiedWeatherResponse(
            latitude=lat,
            longitude=lon,
            snapped_latitude=s_lat,
            snapped_longitude=s_lon,
            department=dept.strip().upper() if dept else None,
            generated_at=datetime.now(tz=UTC),
            current=current,
            observation=obs_station,
            rain_nowcast=now_block,
            vigilance=vig_block,
            hourly=hourly,
            daily=daily,
            sources=sources,
        )

    async def get_compact_weather(
        self,
        lat: float,
        lon: float,
        dept: str | None = None,
    ) -> CompactWeatherResponse:
        """Construit un payload condensé (< 4 Ko) adapté aux microcontrôleurs (ESP32 / LVGL)."""
        full = await self.get_unified_weather(lat=lat, lon=lon, dept=dept, days=2)
        now_ts = int(full.generated_at.timestamp())

        rain_levels = [s.rain_level for s in full.rain_nowcast.slices if s.rain_level is not None]
        rain_1h_max = max(rain_levels) if rain_levels else None

        stale = any(src.status == "stale" for src in full.sources.values())

        next_hours = [
            {
                "t": int(h.time.timestamp()),
                "tc": h.temperature_c,
                "p": h.precipitation_mm,
                "wmo": h.weather_code,
                "w": h.wind_speed_kmh,
            }
            for h in full.hourly[:12]
        ]

        return CompactWeatherResponse(
            lat=full.snapped_latitude,
            lon=full.snapped_longitude,
            dept=full.department,
            ts=now_ts,
            temp_c=full.current.temperature_c if full.current else None,
            feels_c=full.current.apparent_temperature_c if full.current else None,
            hum_pct=full.current.relative_humidity_pct if full.current else None,
            wind_kmh=full.current.wind_speed_kmh if full.current else None,
            gust_kmh=full.current.wind_gusts_kmh if full.current else None,
            wmo=full.current.weather_code if full.current else None,
            vig_lvl=full.vigilance.department.level if full.vigilance.department else None,
            vig_color=full.vigilance.department.color if full.vigilance.department else None,
            rain_1h_max=rain_1h_max,
            stale=stale,
            next_hours=next_hours,
        )

    async def get_rain_nowcast(self, lat: float, lon: float) -> RainNowcastBlock:
        """Retourne la pluie dans l'heure officielle, ou le repli AROME HD 15 min."""
        block, _ = await self._resolve_rain_nowcast(lat=lat, lon=lon)
        return block

    async def get_nearest_observation(
        self,
        lat: float,
        lon: float,
        radius_km: float = 50.0,
    ) -> ObservationsResponse:
        """Retourne l'observation 6 min la plus proche via `DPPaquetObs v2`."""
        if self._obs is None:
            return ObservationsResponse(
                latitude=lat,
                longitude=lon,
                radius_km=radius_km,
                source=SourceStatus(status="not_configured", provider="meteofrance-obs"),
                station=None,
            )
        station, status = await self._obs.get_nearest_observation(lat=lat, lon=lon, max_radius_km=radius_km)
        return ObservationsResponse(
            latitude=lat,
            longitude=lon,
            radius_km=radius_km,
            source=status,
            station=station,
        )
