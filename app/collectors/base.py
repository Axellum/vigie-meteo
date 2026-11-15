"""Classe de base pour les collecteurs résilients (Cache SQLite + Circuit Breaker)."""

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.cache import CacheStore
from app.config import Settings, redact_secret
from app.models import FreshnessStatus, SourceStatus


def snap_coord(value: float, step: float = 0.02) -> float:
    """Aligne une coordonnée sur une grille (~2 km) pour partager le cache entre requêtes proches."""
    return round(round(value / step) * step, 4)


class ResilientCollector:
    """Gère les appels HTTP, le cache SQLite WAL et le circuit breaker d'une source."""

    provider_name: str = "base"

    def __init__(self, client: httpx.AsyncClient, cache: CacheStore, settings: Settings) -> None:
        self._client = client
        self._cache = cache
        self._settings = settings

    async def fetch_with_cache(
        self,
        cache_key: str,
        url: str,
        ttl_seconds: int,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any | None, SourceStatus]:
        """Récupère une ressource JSON avec stratégie Stale-While-Revalidate et circuit breaker.

        Retourne `(payload, SourceStatus)` :
        - Si le cache est frais (< `ttl_seconds`), le retourne immédiatement sans appel réseau.
        - Si le circuit breaker est ouvert, retourne le cache expiré (`stale`) si disponible, sinon `None` (`unavailable`).
        - En cas d'échec réseau/HTTP, enregistre la panne et sert le cache `stale` si présent.
        """
        now_ts = time.time()
        cached = self._cache.get_cache(cache_key)
        if cached is not None:
            payload, updated_ts = cached
            age = max(0, int(now_ts - updated_ts))
            if age < ttl_seconds:
                return payload, self._build_status("fresh", updated_ts, age)

        if self._cache.is_circuit_open(self.provider_name):
            return self._fallback_from_cache(cached, now_ts)

        req_headers = {"User-Agent": self._settings.user_agent}
        if headers:
            req_headers.update(headers)

        try:
            response = await self._client.get(
                url,
                params=params,
                headers=req_headers,
                timeout=self._settings.http_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            err_msg = redact_secret(str(exc), self._settings)
            self._cache.record_failure(
                self.provider_name,
                err_msg,
                self._settings.circuit_fail_threshold,
                self._settings.circuit_cooldown_seconds,
            )
            return self._fallback_from_cache(cached, now_ts, last_error=err_msg)

        updated_ts = self._cache.set_cache(cache_key, self.provider_name, data)
        self._cache.record_success(self.provider_name)
        return data, self._build_status("fresh", updated_ts, 0)

    def _fallback_from_cache(
        self,
        cached: tuple[Any, float] | None,
        now_ts: float,
        last_error: str | None = None,
    ) -> tuple[Any | None, SourceStatus]:
        state = self._cache.get_provider_state(self.provider_name)
        err = last_error or state["last_error"]
        if cached is not None:
            payload, updated_ts = cached
            age = max(0, int(now_ts - updated_ts))
            if age <= self._settings.max_stale_seconds:
                return payload, self._build_status(
                    "stale",
                    updated_ts,
                    age,
                    circuit_open=state["circuit_open"],
                    last_error=err,
                )
        return None, SourceStatus(
            status="unavailable",
            provider=self.provider_name,
            updated_at=state["last_success_at"],
            age_seconds=None,
            circuit_open=state["circuit_open"],
            last_error=err,
        )

    def _build_status(
        self,
        status: FreshnessStatus,
        updated_ts: float,
        age_seconds: int,
        circuit_open: bool = False,
        last_error: str | None = None,
    ) -> SourceStatus:
        return SourceStatus(
            status=status,
            provider=self.provider_name,
            updated_at=datetime.fromtimestamp(updated_ts, tz=UTC),
            age_seconds=age_seconds,
            circuit_open=circuit_open,
            last_error=last_error,
        )
