"""Service d'agrégation météo unifiant Open-Meteo (AROME HD + ARPEGE) et Météo-France."""

import asyncio
from datetime import UTC, datetime

from app.collectors.meteofrance import MeteoFranceNowcastCollector
from app.collectors.open_meteo import OpenMeteoCollector
from app.models import (
    CompactWeatherResponse,
    RainNowcastBlock,
    SourceStatus,
    UnifiedWeatherResponse,
)


class WeatherAggregatorService:
    """Orchestre en parallèle les collecteurs pour produire une réponse unifiée et résiliente."""

    def __init__(
        self,
        open_meteo: OpenMeteoCollector,
        nowcast: MeteoFranceNowcastCollector,
    ) -> None:
        self._om = open_meteo
        self._now = nowcast

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
        days: int = 4,
    ) -> UnifiedWeatherResponse:
        """Agrège prévisions et pluie dans l'heure (les vigilances et les observations sont lues par Vigie)."""
        (om_payload, om_status, s_lat, s_lon), (now_block, now_status) = await asyncio.gather(
            self._om.get_france_forecast(lat=lat, lon=lon, days=days),
            self._resolve_rain_nowcast(lat=lat, lon=lon),
        )

        current = self._om.parse_current(om_payload) if om_payload else None
        hourly = self._om.parse_hourly(om_payload) if om_payload else []
        daily = self._om.parse_daily(om_payload) if om_payload else []

        sources: dict[str, SourceStatus] = {
            "forecast": om_status,
            "rain_nowcast": now_status,
        }

        return UnifiedWeatherResponse(
            latitude=lat,
            longitude=lon,
            snapped_latitude=s_lat,
            snapped_longitude=s_lon,
            generated_at=datetime.now(tz=UTC),
            current=current,
            rain_nowcast=now_block,
            hourly=hourly,
            daily=daily,
            sources=sources,
        )

    async def get_compact_weather(
        self,
        lat: float,
        lon: float,
    ) -> CompactWeatherResponse:
        """Construit un payload condensé (< 4 Ko) adapté aux microcontrôleurs (ESP32 / LVGL)."""
        full = await self.get_unified_weather(lat=lat, lon=lon, days=2)
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
            ts=now_ts,
            temp_c=full.current.temperature_c if full.current else None,
            feels_c=full.current.apparent_temperature_c if full.current else None,
            hum_pct=full.current.relative_humidity_pct if full.current else None,
            wind_kmh=full.current.wind_speed_kmh if full.current else None,
            gust_kmh=full.current.wind_gusts_kmh if full.current else None,
            wmo=full.current.weather_code if full.current else None,
            rain_1h_max=rain_1h_max,
            stale=stale,
            next_hours=next_hours,
        )

    async def get_rain_nowcast(self, lat: float, lon: float) -> RainNowcastBlock:
        """Retourne la pluie dans l'heure officielle, ou le repli AROME HD 15 min."""
        block, _ = await self._resolve_rain_nowcast(lat=lat, lon=lon)
        return block
