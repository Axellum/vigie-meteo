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

    # Portail API officiel Météo-France (Vigilances, Observations 6 min DPPaquetObs v2, Nowcast)
    # Note : `/public/DPPaquetObs/v1/paquet/pluie` ne figure pas dans les Swagger DPPaquetObs v1/v2
    # (la lame d'eau immédiate officielle est servie par PIAF WCS `/pro/piaf/1.0` ou le repli AROME 15 min).
    meteofrance_api_key: SecretStr | None = Field(default=None, alias="METEOFRANCE_API_KEY")
    meteofrance_vigilance_url: str = Field(
        default="https://public-api.meteofrance.fr/public/DPVigilance/v1/cartevigilance/encours",
        alias="METEOFRANCE_VIGILANCE_URL",
    )
    meteofrance_nowcast_url: str = Field(
        default="https://public-api.meteofrance.fr/public/DPPaquetObs/v1/paquet/pluie",
        alias="METEOFRANCE_NOWCAST_URL",
    )
    meteofrance_paquet_obs_url: str = Field(
        default="https://public-api.meteofrance.fr/public/DPPaquetObs/v2/paquet/stations/infrahoraire-6m",
        alias="METEOFRANCE_PAQUET_OBS_URL",
    )

    # Durées de vie du cache (TTL en secondes) et fenêtre de tolérance "stale"
    forecast_ttl_seconds: int = Field(default=900, alias="METEO_FORECAST_TTL_SECONDS")
    vigilance_ttl_seconds: int = Field(default=300, alias="METEO_VIGILANCE_TTL_SECONDS")
    nowcast_ttl_seconds: int = Field(default=180, alias="METEO_NOWCAST_TTL_SECONDS")
    obs_ttl_seconds: int = Field(default=360, alias="METEO_OBS_TTL_SECONDS")
    max_stale_seconds: int = Field(default=86400, alias="METEO_MAX_STALE_SECONDS")

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
