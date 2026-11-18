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
    source_model: str | None = Field(
        default=None,
        description="Modèle ou API source (ex: meteofrance-nowcast, meteofrance_arome_france_hd_15min).",
    )


class RadarFrameInfo(BaseModel):
    """Une image de la mosaïque radar."""

    time: datetime = Field(..., description="Fin des 5 minutes de cumul (UTC).")
    epoch: int = Field(..., description="La même date en secondes UNIX : c'est elle qui adresse les tuiles.")


class RadarLegendClass(BaseModel):
    """Une classe d'intensité de la pluie et sa couleur dans les tuiles."""

    min_mm_h: float = Field(..., description="Intensité minimale de la classe (mm/h) ; elle va jusqu'au minimum de la suivante.")
    color: str = Field(..., description="Couleur `#rrggbb` des tuiles.")
    alpha: float = Field(..., description="Opacité propre de la couleur dans les tuiles (0 à 1).")


class RadarFramesResponse(BaseModel):
    """Les images radar gardées (`GET /v1/radar/frames`), de la plus ancienne à la plus récente."""

    source_model: str
    attribution: str
    interval_minutes: int = 5
    bounds: list[float] = Field(..., description="Ouest, sud, est, nord (degrés) de la zone couverte.")
    min_zoom: int
    max_zoom: int
    tile_path: str = Field(..., description="Modèle de chemin d'une tuile PNG 256 px (Web Mercator) : `{epoch}`, `{z}`, `{x}`, `{y}`.")
    legend: list[RadarLegendClass] = Field(..., description="Les classes d'intensité, de la plus faible à la plus forte.")
    frames: list[RadarFrameInfo]


class ShortTermStep(BaseModel):
    """Une échéance de 15 minutes de la prévision immédiate AROME-PI : ce que le modèle prévoit sur les 15 minutes qui finissent à `time`."""

    time: datetime = Field(..., description="Fin des 15 minutes (UTC).")
    wind_gust_kmh: int = Field(..., description="Rafale maximale des 15 minutes à 10 m (km/h), le maximum du bloc de 0,04° autour du point.")
    hail_kg_m2: float = Field(
        ...,
        description="Diagnostic de grêle AROME (kg/m2, maximum de l'intervalle, maximum du bloc). Météo-France : de 8 à 16 = grésil, 16 et plus = grêle.",
    )
    visibility_m: int = Field(..., description="Visibilité minimale des 15 minutes (m), plafonnée à 20 000, minimum du bloc.")
    precip_type: int = Field(
        ...,
        description="Type de précipitation le plus grave des 15 minutes : code de la table 4.201 de l'OMM (0 aucune, 1 pluie, 3 verglaçante, "
        "5 neige sèche, 6 neige humide, 7 pluie et neige, 8 granules de glace, 9 grésil, 10 grêle, 11 bruine, 12 bruine verglaçante) "
        "ou code local de Météo-France (193 neige collante, 201 à 213 : la même chose, « intermittent »).",
    )


class ShortTermResponse(BaseModel):
    """La prévision immédiate AROME-PI d'un point (`GET /v1/shortterm`)."""

    source_model: str
    attribution: str
    run: datetime = Field(..., description="Heure du run AROME-PI (UTC).")
    interval_minutes: int = 15
    steps: list[ShortTermStep] = Field(..., description="Les échéances qui ne sont pas encore entièrement passées, de la plus proche à la plus lointaine.")


class Attribution(BaseModel):
    """Mentions légales et crédits obligatoires des fournisseurs."""

    forecast: str = "Open-Meteo (CC BY 4.0) / Météo-France AROME & ARPEGE (Licence Ouverte Etalab 2.0)"
    rain_nowcast: str = "Météo-France PIAF (portail-api.meteofrance.fr)"


class UnifiedWeatherResponse(BaseModel):
    """Réponse complète de l'endpoint unifié `GET /v1/weather`."""

    latitude: float
    longitude: float
    snapped_latitude: float
    snapped_longitude: float
    timezone: str = "Europe/Paris"
    generated_at: datetime
    current: CurrentWeather | None = None
    rain_nowcast: RainNowcastBlock
    hourly: list[HourlyForecastPoint] = Field(default_factory=list)
    daily: list[DailyForecastPoint] = Field(default_factory=list)
    sources: dict[str, SourceStatus]
    attribution: Attribution = Field(default_factory=Attribution)


class CompactWeatherResponse(BaseModel):
    """Réponse compacte (< 4 Ko) optimisée pour ESP32 / LVGL / écrans embarqués."""

    lat: float
    lon: float
    ts: int = Field(..., description="Timestamp UNIX UTC.")
    temp_c: float | None = None
    feels_c: float | None = None
    hum_pct: int | None = None
    wind_kmh: float | None = None
    gust_kmh: float | None = None
    wmo: int | None = None
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
