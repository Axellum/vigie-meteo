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

    # Durées de vie du cache (TTL en secondes) et fenêtre de tolérance "stale"
    forecast_ttl_seconds: int = Field(default=900, alias="METEO_FORECAST_TTL_SECONDS")
    nowcast_ttl_seconds: int = Field(default=180, alias="METEO_NOWCAST_TTL_SECONDS")
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
