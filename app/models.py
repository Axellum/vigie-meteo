"""Modèles de données Pydantic v2 normalisés selon les standards ouverts.

Conventions appliquées :
- Aucune donnée fabriquée : une information absente ou indisponible vaut `None` (`null` en JSON).
- Horodatages au format ISO-8601 UTC.
- Codes météo WMO 4677 standardisés.
- Erreurs HTTP conformes à la RFC 9457 (`application/problem+json`).
"""

from datetime import date, datetime
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
    """Les images radar gardées (`GET /v1/radar/{area}/frames`), de la plus ancienne à la plus récente."""

    area: str = Field(default="france", description="La zone : `france` (métropole), `antilles`, `reunion` ou `new-caledonia`.")
    source_model: str
    attribution: str
    interval_minutes: int = 5
    bounds: list[float] = Field(..., description="Ouest, sud, est, nord (degrés) de la zone couverte.")
    min_zoom: int
    max_zoom: int
    tile_path: str = Field(..., description="Modèle de chemin d'une tuile PNG 256 px (Web Mercator) : `{epoch}`, `{z}`, `{x}`, `{y}`.")
    legend: list[RadarLegendClass] = Field(..., description="Les classes d'intensité, de la plus faible à la plus forte.")
    frames: list[RadarFrameInfo]


class RainNowcastFrameInfo(BaseModel):
    """Une échéance de la carte pluie dans l'heure PIAF (+5 à +60 min)."""

    epoch: int = Field(..., description="Fin des 5 minutes de cumul en secondes UNIX UTC : adresse les tuiles.")
    time_utc: datetime = Field(..., description="Fin des 5 minutes de cumul (ISO-8601 UTC).")
    lead_minutes: int = Field(..., description="Échéance en minutes après l'heure du run (5, 10, …, 60).")


class RainNowcastFramesResponse(BaseModel):
    """Les échéances du run PIAF en mémoire (`GET /v1/rain-nowcast/frames`), de +5 min à +60 min."""

    source_model: str = Field(..., description="Modèle source (`meteofrance-piaf`).")
    run_utc: datetime = Field(..., description="Heure du run PIAF en mémoire (ISO-8601 UTC).")
    bounds: list[float] = Field(..., description="Ouest, sud, est, nord (degrés WGS84) de la zone couverte.")
    min_zoom: int = Field(..., description="Zoom Web Mercator minimal servi (3).")
    max_zoom: int = Field(..., description="Zoom Web Mercator maximal servi (10).")
    step_seconds: int = Field(..., description="Pas temporel entre deux échéances en secondes (300).")
    tile_path: str = Field(..., description="Modèle de chemin d'une tuile PNG 256 px (Web Mercator) : `{epoch}`, `{z}`, `{x}`, `{y}`.")
    legend: list[RadarLegendClass] = Field(..., description="Les classes d'intensité (identiques au radar), de la plus faible à la plus forte.")
    frames: list[RainNowcastFrameInfo] = Field(..., description="Les échéances du run courant, par ordre chronologique.")
    attribution: str = Field(..., description="Crédit obligatoire de la source Météo-France.")


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
    rain_mm: float | None = Field(
        None,
        description="Cumul des précipitations des 15 minutes (mm, pluie et neige fondue), moyenne du bloc de 0,04° autour du point. "
        "null pour un run lu avant que la passerelle garde ce cumul.",
    )


class ShortTermResponse(BaseModel):
    """La prévision immédiate AROME-PI d'un point (`GET /v1/shortterm`)."""

    source_model: str
    attribution: str
    run: datetime = Field(..., description="Heure du run AROME-PI (UTC).")
    interval_minutes: int = 15
    steps: list[ShortTermStep] = Field(..., description="Les échéances qui ne sont pas encore entièrement passées, de la plus proche à la plus lointaine.")


class ProbabilityWindow(BaseModel):
    """Les probabilités d'une fenêtre qui finit à `time` (UTC) et dure `window_hours` heures."""

    time: datetime = Field(..., description="Fin de la fenêtre (UTC).")
    window_hours: int = Field(..., description="Durée de la fenêtre en heures : 3 (PE-AROME) ou 24 (PE-ARPEGE).")
    probability_pct: dict[str, int] = Field(
        ...,
        description="Pourcentage par grandeur, de 0 à 100. `rain_1mm`, `rain_5mm`, `rain_10mm`, `rain_20mm` : cumul de pluie de la fenêtre au moins égal à "
        "ce seuil ; `snow_1mm` : cumul de neige d'au moins 1 mm ; `gust_30kt`, `gust_50kt` (nœuds), `hail` (diagnostic AROME > 8 kg/m2) et `storm` "
        "(réflectivité maximale > 40 dBZ) : probabilité à l'instant où finit la fenêtre, pas sur les 3 h.",
    )


class EnsembleBlock(BaseModel):
    """Les fenêtres d'une prévision d'ensemble de Météo-France autour d'un point (la probabilité du bloc de 5 km ou de la maille de 10 km qui le contient)."""

    source_model: str
    attribution: str
    run: datetime = Field(..., description="Heure du run (UTC).")
    members: int = Field(..., description="Nombre de membres de l'ensemble (35).")
    windows: list[ProbabilityWindow] = Field(..., description="Les fenêtres qui ne sont pas encore entièrement passées, de la plus proche à la plus lointaine.")


class ProbabilitiesResponse(BaseModel):
    """Les probabilités des prévisions d'ensemble PE-AROME (3 h, jusqu'à +51 h) et PE-ARPEGE (24 h, jusqu'à +96 h) d'un point (`GET /v1/probabilities`)."""

    arome: EnsembleBlock | None = Field(None, description="Absent sans run récent de PE-AROME.")
    arpege: EnsembleBlock | None = Field(None, description="Absent sans run récent de PE-ARPEGE.")


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


class OverseasStep(BaseModel):
    """Une heure de la prévision AROME-OM d'un point : `time` (UTC) est l'heure de validité."""

    time: datetime
    temperature_c: float | None = None
    humidity_pct: float | None = None
    wind_kmh: float | None = Field(None, description="Vent moyen à 10 m.")
    wind_direction_deg: float | None = Field(None, description="Direction d'où vient le vent.")
    gust_kmh: float | None = Field(None, description="Rafale maximale de l'heure qui finit à `time`.")
    pressure_hpa: float | None = Field(None, description="Pression réduite au niveau de la mer.")
    cloud_cover_pct: float | None = None
    precip_mm: float | None = Field(None, description="Pluie de l'heure qui finit à `time` (différence de deux cumuls du modèle).")


class OverseasResponse(BaseModel):
    """La prévision horaire AROME-OM d'un point d'outre-mer (`GET /v1/overseas`)."""

    source_model: str
    attribution: str
    window: str = Field(..., description="La fenêtre servie qui contient le point (nom).")
    utc_offset_s: int = Field(..., description="Décalage horaire du territoire (fixe, sans heure d'été).")
    run: datetime = Field(..., description="Heure du run le plus récent (UTC) ; les heures avant lui viennent du run précédent.")
    nearest_node: dict[str, float] = Field(..., description="Latitude et longitude du nœud de la grille de 0,025° (environ 2,8 km) dont les valeurs sont servies.")
    steps: list[OverseasStep] = Field(..., description="Les heures de validité de moins d'une heure dans le passé à +48 h après le run, de la plus proche à la plus lointaine.")


class StationDay(BaseModel):
    """Le relevé quotidien d'une station pour un jour (`date`, jour civil UTC de la mesure ; la pluie est celle de 06 h UTC ce jour à 06 h UTC le lendemain)."""

    date: date
    rain_mm: float | None = Field(
        None, description="Cumul de précipitations sur 24 h (de 06 h UTC le jour J à 06 h UTC le jour J+1)."
    )
    tmin_c: float | None = Field(None, description="Température minimale sous abri.")
    tmax_c: float | None = Field(None, description="Température maximale sous abri.")
    tmean_c: float | None = Field(None, description="Moyenne quotidienne des températures horaires sous abri.")
    wind_mean_ms: float | None = Field(None, description="Moyenne quotidienne du vent moyenné sur 10 min à 10 m (m/s).")
    gust_ms: float | None = Field(None, description="Maximum quotidien du vent instantané à 10 m (m/s).")
    doubtful: list[str] = Field(
        default_factory=list,
        description="Grandeurs écartées (`null`) parce que Météo-France les marque douteuses (code qualité 2).",
    )


class StationDailyResponse(BaseModel):
    """Les derniers jours mesurés par une station Météo-France (`GET /v1/station-daily`)."""

    source_model: str
    attribution: str
    station: str = Field(..., description="Numéro Météo-France de la station (8 chiffres).")
    last_day: date | None = Field(
        None, description="Le dernier jour que le portail pouvait donner à ce moment (hier après 11 h 30 UTC)."
    )
    fetched_at: datetime | None = Field(None, description="Quand le portail a été interrogé pour cette station.")
    days: list[StationDay] = Field(
        ..., description="Du plus ancien au plus récent ; vide si la station n'a rien mesuré sur la période."
    )


class ForestDepartment(BaseModel):
    """Le niveau de danger de feux de forêt d'un département : 1 faible, 2 modéré, 3 élevé, 4 très élevé."""

    department: str = Field(..., description="Code du département (`01`, `2A`, `13`...).")
    name: str | None = None
    level_d1: int | None = Field(None, description="Niveau du jour J+1.")
    level_d2: int | None = Field(None, description="Niveau du jour J+2.")


class ForestsResponse(BaseModel):
    """La Météo des forêts (`GET /v1/forests`) : `available` quand le portail publie, `out_of_season` sinon (le flux est saisonnier, de juin à septembre)."""

    source_model: str
    attribution: str
    status: Literal["available", "out_of_season"]
    reference_time: datetime | None = Field(
        None, description="Date de la publication servie (UTC) ; `null` hors saison."
    )
    checked_at: datetime | None = Field(None, description="Dernière fois que le portail a été interrogé.")
    departments: list[ForestDepartment] = Field(default_factory=list)
