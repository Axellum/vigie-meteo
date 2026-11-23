"""Configuration centralisée via variables d'environnement (pydantic-settings).

Les secrets (clés API) ne sont jamais journalisés ni exposés dans les réponses HTTP.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Paramètres d'exécution de l'API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="production", alias="METEO_ENV")
    log_level: str = Field(default="INFO", alias="METEO_LOG_LEVEL")
    db_path: str = Field(default="./meteo_cache.db", alias="METEO_DB_PATH")

    # Sécurité : si défini, exige X-API-Key ou Authorization: Bearer sur les routes protégées.
    api_key: SecretStr | None = Field(default=None, alias="METEO_API_KEY")

    # Open-Meteo (instance locale Docker par défaut, avec repli optionnel)
    open_meteo_base_url: str = Field(default="http://open-meteo:8080", alias="OPEN_METEO_BASE_URL")
    open_meteo_fallback_url: str | None = Field(
        default="https://api.open-meteo.com",
        alias="OPEN_METEO_FALLBACK_URL",
    )

    # Portail API officiel Météo-France : pluie PIAF seulement (vigilances et observations : lues par Vigie elle-même)
    meteofrance_api_key: SecretStr | None = Field(default=None, alias="METEOFRANCE_API_KEY")
    # Pluie dans l'heure : PIAF (Prévision Immédiate Agrégée Fusionnée), WCS GetCoverage.
    # Vérifié le 16/11/2026 : l'hôte est `api.meteofrance.fr` (sur `public-api`, connexion coupée) ;
    # un run toutes les 5 min, publié ~10-13 min après son heure ; échéances de 300 s à 11 700 s ;
    # une requête = une échéance (une plage `time(a,b)` renvoie 404) ; un point `lat(x)&long(y)`
    # renvoie un petit XML GML dont la valeur est le cumul en mm sur 5 min (comparé aux cumuls
    # 6 min des stations DPPaquetObs sous la pluie le même soir). L'ancienne URL
    # `DPPaquetObs/v1/paquet/pluie` n'a jamais existé (404) : `METEOFRANCE_NOWCAST_URL` est ignorée.
    meteofrance_piaf_url: str = Field(
        default="https://api.meteofrance.fr/pro/piaf/1.0/wcs/MF-NWP-HIGHRES-PIAF-001-FRANCE-WCS/GetCoverage",
        alias="METEOFRANCE_PIAF_URL",
    )
    piaf_steps: int = Field(default=12, ge=1, le=39, alias="METEO_PIAF_STEPS")
    piaf_publish_delay_minutes: int = Field(default=10, ge=0, le=60, alias="METEO_PIAF_PUBLISH_DELAY_MINUTES")
    piaf_ttl_seconds: int = Field(default=300, alias="METEO_PIAF_TTL_SECONDS")
    piaf_concurrency: int = Field(default=4, ge=1, le=12, alias="METEO_PIAF_CONCURRENCY")
    # La carte PIAF d'un run (toute la France) reste servie, pendant qu'un run plus récent se télécharge en tâche de
    # fond, tant qu'elle a moins de cet âge ; au-delà, la requête attend le téléchargement (ou bascule en repli).
    piaf_grid_max_stale_seconds: int = Field(default=900, ge=60, alias="METEO_PIAF_GRID_MAX_STALE_SECONDS")
    # Le run PIAF rangé sur disque pour que les deux processus n'en téléchargent qu'un (défaut : `piaf/` à côté de la base SQLite).
    piaf_dir: str | None = Field(default=None, alias="METEO_PIAF_DIR")
    piaf_keep_runs: int = Field(default=2, ge=1, le=12, alias="METEO_PIAF_KEEP_RUNS")

    # Mosaïque radar « lame d'eau » (DPRadar, métropole) : images gardées sur disque pour l'animation et les tuiles.
    radar_enabled: bool = Field(default=True, alias="METEO_RADAR_ENABLED")
    radar_observation_url: str = Field(
        default="https://public-api.meteofrance.fr/public/DPRadar/v1/mosaiques/METROPOLE/observations/LAME_D_EAU",
        alias="METEOFRANCE_RADAR_URL",
    )
    radar_dir: str | None = Field(default=None, alias="METEO_RADAR_DIR")  # défaut : `radar/` à côté de la base SQLite
    radar_keep_minutes: int = Field(default=180, ge=15, le=1440, alias="METEO_RADAR_KEEP_MINUTES")
    # Les mosaïques lues (noms de nos adresses, séparés par des virgules) : la métropole et les trois zones d'outre-mer de DPRadar.
    radar_areas: str = Field(default="france,antilles,reunion,new-caledonia", alias="METEO_RADAR_AREAS")

    # AROME-PI (prévision immédiate, WCS `aromepi/1.0`) : grêle, rafales, visibilité et type de précipitation, 24 échéances de 15 min.
    # Un run par heure, publié ~20 min après son heure (relevé le 18/11/2026) ; une échéance par requête (96 requêtes par run).
    aromepi_enabled: bool = Field(default=True, alias="METEO_AROMEPI_ENABLED")
    meteofrance_aromepi_url: str = Field(
        default="https://public-api.meteofrance.fr/public/aromepi/1.0/wcs/MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS",
        alias="METEOFRANCE_AROMEPI_URL",
    )
    aromepi_dir: str | None = Field(default=None, alias="METEO_AROMEPI_DIR")  # défaut : `aromepi/` à côté de la base SQLite
    aromepi_publish_delay_minutes: int = Field(default=20, ge=0, le=120, alias="METEO_AROMEPI_PUBLISH_DELAY_MINUTES")
    # Le quota du portail est de 50 requêtes par minute pour toute la clé (PIAF et le radar en prennent aussi) : 20 par minute et par
    # processus, un seul des deux processus télécharge (verrou de fichier).
    aromepi_requests_per_minute: int = Field(default=20, ge=1, le=40, alias="METEO_AROMEPI_REQUESTS_PER_MINUTE")
    aromepi_concurrency: int = Field(default=2, ge=1, le=8, alias="METEO_AROMEPI_CONCURRENCY")
    aromepi_keep_runs: int = Field(default=3, ge=1, le=24, alias="METEO_AROMEPI_KEEP_RUNS")
    # Au-delà de cet âge le dernier run n'est plus servi (il ne couvrirait plus que quelques échéances à venir).
    aromepi_max_age_minutes: int = Field(default=180, ge=30, le=1440, alias="METEO_AROMEPI_MAX_AGE_MINUTES")

    # Prévisions d'ensemble (probabilités) : PE-AROME (`pearome/1.0`, +51 h, runs à 03/09/15/21 h UTC) et PE-ARPEGE (`pearpege/1.0`, un service par
    # membre, +96 h lus, runs à 00/06/12/18 h UTC). Délais de publication à affiner à la mesure ; PE-AROME = 119 requêtes par run, PE-ARPEGE = 140.
    pearome_enabled: bool = Field(default=True, alias="METEO_PEAROME_ENABLED")
    meteofrance_pearome_url: str = Field(
        default="https://public-api.meteofrance.fr/public/pearome/1.0/wcs/MF-NWP-HIGHRES-PEAROME-0025-FRANCE-WCS",
        alias="METEOFRANCE_PEAROME_URL",
    )
    pearome_dir: str | None = Field(default=None, alias="METEO_PEAROME_DIR")  # défaut : `pearome/` à côté de la base SQLite
    pearome_publish_delay_minutes: int = Field(default=120, ge=0, le=720, alias="METEO_PEAROME_PUBLISH_DELAY_MINUTES")
    pearome_requests_per_minute: int = Field(default=12, ge=1, le=30, alias="METEO_PEAROME_REQUESTS_PER_MINUTE")
    pearome_concurrency: int = Field(default=2, ge=1, le=6, alias="METEO_PEAROME_CONCURRENCY")
    pearome_keep_runs: int = Field(default=3, ge=1, le=12, alias="METEO_PEAROME_KEEP_RUNS")
    pearome_max_age_minutes: int = Field(default=840, ge=60, le=2880, alias="METEO_PEAROME_MAX_AGE_MINUTES")
    pearp_enabled: bool = Field(default=True, alias="METEO_PEARP_ENABLED")
    meteofrance_pearp_url: str = Field(
        default="https://public-api.meteofrance.fr/public/pearpege/1.0/wcs/MF-NWP-GLOBAL-PEARP{member}-01-EUROPE-WCS",
        alias="METEOFRANCE_PEARP_URL",
    )
    pearp_dir: str | None = Field(default=None, alias="METEO_PEARP_DIR")  # défaut : `pearp/` à côté de la base SQLite
    pearp_publish_delay_minutes: int = Field(default=180, ge=0, le=720, alias="METEO_PEARP_PUBLISH_DELAY_MINUTES")
    pearp_requests_per_minute: int = Field(default=10, ge=1, le=30, alias="METEO_PEARP_REQUESTS_PER_MINUTE")
    pearp_concurrency: int = Field(default=2, ge=1, le=6, alias="METEO_PEARP_CONCURRENCY")
    pearp_keep_runs: int = Field(default=2, ge=1, le=12, alias="METEO_PEARP_KEEP_RUNS")
    pearp_max_age_minutes: int = Field(default=1200, ge=60, le=2880, alias="METEO_PEARP_MAX_AGE_MINUTES")

    # AROME-OM (PaquetAROME-OM, les cinq modèles d'outre-mer à 0,025°) : température, humidité, vent, rafales, pression, nébulosité et pluie,
    # 49 échéances horaires, 4 runs par jour publiés 6 h 20 à 8 h après leur heure (délai de 8 h 30 : tous les territoires sont alors complets) (relevé le 18/11/2026). Un fichier par échéance et par territoire.
    aromeom_enabled: bool = Field(default=True, alias="METEO_AROMEOM_ENABLED")
    meteofrance_aromeom_url: str = Field(default="https://public-api.meteofrance.fr/previnum/DPPaquetAROME-OM/v1", alias="METEOFRANCE_AROMEOM_URL")
    aromeom_dir: str | None = Field(default=None, alias="METEO_AROMEOM_DIR")  # défaut : `aromeom/` à côté de la base SQLite
    aromeom_publish_delay_minutes: int = Field(default=510, ge=0, le=1440, alias="METEO_AROMEOM_PUBLISH_DELAY_MINUTES")
    aromeom_requests_per_minute: int = Field(default=10, ge=1, le=30, alias="METEO_AROMEOM_REQUESTS_PER_MINUTE")
    aromeom_keep_runs: int = Field(default=2, ge=2, le=6, alias="METEO_AROMEOM_KEEP_RUNS")
    # Au-delà de cet âge le dernier run n'est plus servi.
    aromeom_max_age_minutes: int = Field(default=1200, ge=120, le=2880, alias="METEO_AROMEOM_MAX_AGE_MINUTES")

    # Relevés quotidiens d'une station (DPClim, commande asynchrone puis fichier CSV) : une commande (3 à 4 requêtes) par station et par jour au plus,
    # étalée pour laisser le quota de 50 par minute aux autres sources. Avec deux processus, le plafond réel est le double.
    dpclim_enabled: bool = Field(default=True, alias="METEO_DPCLIM_ENABLED")
    meteofrance_dpclim_url: str = Field(
        default="https://public-api.meteofrance.fr/public/DPClim/v1", alias="METEOFRANCE_DPCLIM_URL"
    )
    dpclim_commands_per_minute: int = Field(default=6, ge=1, le=20, alias="METEO_DPCLIM_COMMANDS_PER_MINUTE")
    # Au-delà de cette attente pour passer devant les autres commandes, la route répond 503 (le client réessaie plus tard).
    dpclim_max_wait_seconds: float = Field(default=10.0, ge=0.0, le=60.0, alias="METEO_DPCLIM_MAX_WAIT_SECONDS")

    # Météo des forêts (DPMeteoForets) : danger de feux par département, J+1 et J+2, publié l'été (juin à septembre) vers 17 h locales.
    # Hors saison le portail répond 404 : ce n'est pas une panne.
    forests_enabled: bool = Field(default=True, alias="METEO_FORESTS_ENABLED")
    meteofrance_forests_url: str = Field(
        default="https://public-api.meteofrance.fr/public/DPMeteoForets/v1", alias="METEOFRANCE_FORESTS_URL"
    )
    forests_poll_minutes: int = Field(default=30, ge=5, le=240, alias="METEO_FORESTS_POLL_MINUTES")
    forests_off_season_poll_minutes: int = Field(
        default=360, ge=30, le=1440, alias="METEO_FORESTS_OFF_SEASON_POLL_MINUTES"
    )

    # Durées de vie du cache (TTL en secondes) et fenêtre de tolérance "stale"
    forecast_ttl_seconds: int = Field(default=900, alias="METEO_FORECAST_TTL_SECONDS")
    nowcast_ttl_seconds: int = Field(default=180, alias="METEO_NOWCAST_TTL_SECONDS")
    max_stale_seconds: int = Field(default=86400, alias="METEO_MAX_STALE_SECONDS")
    # Purge des entrées du cache SQLite plus vieilles que `max_stale_seconds` (elles ne peuvent plus être servies) : toutes les 6 h.
    cache_purge_interval_seconds: int = Field(default=21600, ge=60, alias="METEO_CACHE_PURGE_INTERVAL_SECONDS")

    # Circuit breaker (protection contre les 429 / 502 / timeouts amont)
    circuit_fail_threshold: int = Field(default=3, alias="METEO_CIRCUIT_FAIL_THRESHOLD")
    circuit_cooldown_seconds: int = Field(default=120, alias="METEO_CIRCUIT_COOLDOWN_SECONDS")

    # Délais réseau et User-Agent
    http_timeout_seconds: float = Field(default=8.0, alias="METEO_HTTP_TIMEOUT_SECONDS")
    user_agent: str = Field(default="meteo-france-api/0.1.0 (self-hosted)", alias="METEO_USER_AGENT")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retourne l'instance unique de configuration (mise en cache)."""
    return Settings()


def redact_secret(text: str, settings: Settings) -> str:
    """Masque toute occurrence des secrets connus dans une chaîne d'erreur ou de log."""
    cleaned = text
    for secret in (settings.api_key, settings.meteofrance_api_key):
        if secret is not None:
            raw = secret.get_secret_value()
            if raw:
                cleaned = cleaned.replace(raw, "***REDACTED***")
    return cleaned
