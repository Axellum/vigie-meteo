"""Modèles de données Pydantic v2 normalisés selon les standards ouverts.

Conventions appliquées :
- Aucune donnée fabriquée : une information absente ou indisponible vaut `None` (`null` en JSON).
- Horodatages au format ISO-8601 UTC.
- Codes météo WMO 4677 standardisés.
- Erreurs HTTP conformes à la RFC 9457 (`application/problem+json`).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

FreshnessStatus = Literal["fresh", "stale", "unavailable", "not_configured"]
VigilanceColor = Literal["green", "yellow", "orange", "red"]


class ProblemDetail(BaseModel):
    """Réponse d'erreur normalisée RFC 9457 (Problem Details for HTTP APIs)."""

    type: str = Field(default="about:blank", description="URI identifiant le type d'erreur.")
    title: str = Field(..., description="Résumé court et stable du problème.")
    status: int = Field(..., description="Code HTTP.")
    detail: str | None = Field(default=None, description="Explication contextuelle sans fuite de secret.")
    instance: str | None = Field(default=None, description="Chemin de la requête concernée.")


class SourceStatus(BaseModel):
    """État de fraîcheur et de santé d'une source de données."""

    status: FreshnessStatus
    provider: str
    updated_at: datetime | None = None
    age_seconds: int | None = None
    circuit_open: bool = False
    last_error: str | None = None


class CurrentWeather(BaseModel):
    """Conditions météorologiques actuelles (unités SI / métriques standard)."""

    time: datetime
    temperature_c: float | None = None
    apparent_temperature_c: float | None = None
    relative_humidity_pct: int | None = None
    precipitation_mm: float | None = None
    wind_speed_kmh: float | None = None
    wind_gusts_kmh: float | None = None
    wind_direction_deg: int | None = None
    pressure_hpa: float | None = None
    cloud_cover_pct: int | None = None
    weather_code: int | None = Field(default=None, description="Code WMO 4677.")
    is_day: bool | None = None
    source_model: str | None = None


class HourlyForecastPoint(BaseModel):
    """Point de prévision horaire issu de la fusion AROME HD + ARPEGE."""

    time: datetime
    temperature_c: float | None = None
    apparent_temperature_c: float | None = None
    relative_humidity_pct: int | None = None
    precipitation_mm: float | None = None
    precipitation_probability_pct: int | None = None
    wind_speed_kmh: float | None = None
    wind_gusts_kmh: float | None = None
    wind_direction_deg: int | None = None
    cloud_cover_pct: int | None = None
    pressure_hpa: float | None = None
    uv_index: float | None = None
    shortwave_radiation_wm2: float | None = None
    weather_code: int | None = Field(default=None, description="Code WMO 4677.")
    source_model: str = Field(..., description="Modèle source (ex: meteofrance_arome_france_hd).")


class DailyForecastPoint(BaseModel):
    """Point de prévision journalière agrégé."""

    date: str = Field(..., description="Date locale ISO (YYYY-MM-DD).")
    temperature_min_c: float | None = None
    temperature_max_c: float | None = None
    precipitation_sum_mm: float | None = None
    wind_speed_max_kmh: float | None = None
    wind_gusts_max_kmh: float | None = None
    uv_index_max: float | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None
    weather_code: int | None = None


class RainNowcastSlice(BaseModel):
    """Tranche de prévision pluie dans l'heure (5 à 10 min) issue de Météo-France."""

    time: datetime
    rain_level: int | None = Field(default=None, description="Indice Météo-France (1=sec, 2=faible, 3=modérée, 4=forte).")
    rain_label: str | None = Field(default=None, description="Libellé officiel Météo-France.")
    rain_mm_h: float | None = None


class RainNowcastBlock(BaseModel):
    """Bloc complet Pluie dans l'heure."""

    status: FreshnessStatus
    slices: list[RainNowcastSlice] = Field(default_factory=list)
    updated_at: datetime | None = None


class VigilancePhenomenon(BaseModel):
    """Phénomène de vigilance officiel (ex: Orages, Vent, Pluie-Inondation)."""

    id: int | str
    name: str
    color: VigilanceColor
    level: int = Field(..., ge=1, le=4, description="1=Vert, 2=Jaune, 3=Orange, 4=Rouge.")
    start_time: datetime | None = None
    end_time: datetime | None = None


class VigilanceDepartment(BaseModel):
    """État de vigilance officiel pour un département français."""

    department: str = Field(..., description="Code département (ex: 75, 40, 2A).")
    color: VigilanceColor
    level: int = Field(..., ge=1, le=4)
    phenomena: list[VigilancePhenomenon] = Field(default_factory=list)
    issued_at: datetime | None = None
    valid_until: datetime | None = None


class VigilanceBlock(BaseModel):
    """Bloc Vigilances avec statut de fraîcheur."""

    status: FreshnessStatus
    department: VigilanceDepartment | None = None


class Attribution(BaseModel):
    """Mentions légales et crédits obligatoires des fournisseurs."""

    forecast: str = "Open-Meteo (CC BY 4.0) / Météo-France AROME & ARPEGE (Licence Ouverte Etalab 2.0)"
    vigilance: str = "Météo-France (Licence Ouverte Etalab 2.0)"


class UnifiedWeatherResponse(BaseModel):
    """Réponse complète de l'endpoint unifié `GET /v1/weather`."""

    latitude: float
    longitude: float
    snapped_latitude: float
    snapped_longitude: float
    department: str | None = None
    timezone: str = "Europe/Paris"
    generated_at: datetime
    current: CurrentWeather | None = None
    rain_nowcast: RainNowcastBlock
    vigilance: VigilanceBlock
    hourly: list[HourlyForecastPoint] = Field(default_factory=list)
    daily: list[DailyForecastPoint] = Field(default_factory=list)
    sources: dict[str, SourceStatus]
    attribution: Attribution = Field(default_factory=Attribution)


class CompactWeatherResponse(BaseModel):
    """Réponse compacte (< 4 Ko) optimisée pour ESP32 / LVGL / écrans embarqués."""

    lat: float
    lon: float
    dept: str | None = None
    ts: int = Field(..., description="Timestamp UNIX UTC.")
    temp_c: float | None = None
    feels_c: float | None = None
    hum_pct: int | None = None
    wind_kmh: float | None = None
    gust_kmh: float | None = None
    wmo: int | None = None
    vig_lvl: int | None = Field(default=None, description="1=Vert, 2=Jaune, 3=Orange, 4=Rouge.")
    vig_color: VigilanceColor | None = None
    rain_1h_max: int | None = Field(default=None, description="Niveau max pluie dans l'heure (1..4).")
    stale: bool = False
    next_hours: list[dict[str, Any]] = Field(default_factory=list, description="Prochaines 12h condensées.")


class HealthResponse(BaseModel):
    """État de santé global de l'API (`GET /v1/health`)."""

    status: Literal["ok", "degraded"]
    version: str
    db_ok: bool
    api_key_required: bool
    sources: dict[str, SourceStatus]
