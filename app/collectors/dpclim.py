"""Relevés quotidiens d'une station Météo-France (`DPClim/v1`) : « hier et les derniers jours à la station la plus proche ».

L'API est **asynchrone** : `commande-station/quotidienne` répond 202 avec un numéro de commande, puis `commande/fichier` répond 204 tant que le
fichier se prépare et 201 avec le CSV quand il est prêt (relevé le 18/11/2026 : prêt à la deuxième interrogation, 1,6 s après). Le CSV a un point-virgule
pour séparateur, la virgule pour décimale, une ligne par jour et une colonne par paramètre suivie de son code qualité (`RR;QRR;...`).

Limites du portail (`API-Doc_DonneesClimatologiques`) : une période d'un an au plus ; hier (J-1) n'est disponible qu'**après 11 h 30 UTC** aujourd'hui, avant
cela le dernier jour est J-2. Les stations fermées gardent leurs archives. Une station sans mesure sur la période donne une erreur 500 à la récupération
du fichier, une station inconnue un 400 à la commande (404 relevé le 18/11/2026 pour un numéro inexistant).

Cache : une entrée par station (`dpclim:<id>`, dans la base SQLite) qui garde les 14 derniers jours jusqu'au dernier jour disponible ; elle est refaite au plus une
fois par jour (quand le dernier jour disponible avance). Une station sans donnée est gardée vide pour la journée, sans réinterroger. Les commandes sont étalées
(`METEO_DPCLIM_COMMANDS_PER_MINUTE`) pour laisser le quota de 50 requêtes par minute aux autres sources ; une commande qui devrait attendre trop longtemps
est refusée (`BusyError`, la route répond 503).
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.cache import CacheStore
from app.config import Settings, redact_secret
from app.models import StationDay

log = logging.getLogger(__name__)

PROVIDER = "meteofrance-dpclim"
SOURCE_MODEL = "meteofrance-dpclim"
ATTRIBUTION = "Source : Météo-France, données climatologiques de base, Licence Ouverte 2.0"
WINDOW_DAYS = 14
AVAILABLE_AFTER_UTC = (11, 30)  # hier n'est disponible qu'à partir de cette heure UTC
POLL_EVERY_S = 1.5
MAX_POLLS = 10

# colonne du CSV -> champ de StationDay (les colonnes qualité s'appellent `Q` + le nom)
COLUMNS = {
    "RR": "rain_mm",
    "TN": "tmin_c",
    "TX": "tmax_c",
    "TM": "tmean_c",
    "FFM": "wind_mean_ms",
    "FXI": "gust_ms",
}
DOUBTFUL = "2"  # « donnée douteuse en cours de vérification » (descriptif des données quotidiennes de base)


class NoDataError(Exception):
    """La station est inconnue ou n'a rien mesuré sur la période : réponse vide, gardée pour la journée."""


class UpstreamError(Exception):
    """Le portail n'a pas répondu comme attendu."""


class BusyError(Exception):
    """Trop de commandes en attente : réessayer plus tard."""


def last_available_day(now: datetime) -> date:
    """Le dernier jour que le portail peut donner : hier après 11 h 30 UTC, avant-hier avant."""
    now = now.astimezone(UTC)
    cutoff = now.replace(hour=AVAILABLE_AFTER_UTC[0], minute=AVAILABLE_AFTER_UTC[1], second=0, microsecond=0)
    return (now - timedelta(days=1 if now >= cutoff else 2)).date()


def _number(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def parse_daily_csv(text: str) -> list[StationDay]:
    """Les jours d'un CSV de commande quotidienne, du plus ancien au plus récent. Une valeur au code qualité 2 est écartée et nommée dans `doubtful`."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=";")
    days: list[StationDay] = []
    for row in reader:
        stamp = (row.get("DATE") or "").strip()
        try:
            day = datetime.strptime(stamp, "%Y%m%d").date()
        except ValueError:
            continue
        values: dict[str, Any] = {}
        doubtful: list[str] = []
        for column, field in COLUMNS.items():
            value = _number(row.get(column) or "")
            if value is not None and (row.get("Q" + column) or "").strip() == DOUBTFUL:
                value = None
                doubtful.append(field)
            values[field] = value
        days.append(StationDay(date=day, doubtful=doubtful, **values))
    days.sort(key=lambda d: d.date)
    return days


class StationDailyCollector:
    """Les relevés quotidiens d'une station, avec un cache par station et par jour."""

    def __init__(self, client: httpx.AsyncClient, cache: CacheStore, settings: Settings) -> None:
        self._client = client
        self._cache = cache
        self._settings = settings
        self._next_at = 0.0
        self._pace_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self.last_error: str | None = None

    def enabled(self) -> bool:
        key = self._settings.meteofrance_api_key
        return self._settings.dpclim_enabled and key is not None and bool(key.get_secret_value().strip())

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._settings.meteofrance_api_key.get_secret_value().strip(),  # type: ignore[union-attr]
            "accept": "*/*",
            "User-Agent": self._settings.user_agent,
        }

    async def _reserve_command_slot(self) -> None:
        """Étale les commandes ; refuse si l'attente dépasse le plafond (le portail n'est pas appelé)."""
        interval = 60.0 / self._settings.dpclim_commands_per_minute
        async with self._pace_lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            if start - now > self._settings.dpclim_max_wait_seconds:
                raise BusyError("trop de commandes DPClim en attente")
            self._next_at = start + interval
        if start > now:
            await asyncio.sleep(start - now)

    async def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        return await self._client.get(
            f"{self._settings.meteofrance_dpclim_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=self._settings.http_timeout_seconds * 2,
        )

    async def _download(self, station: str, first: date, last: date) -> str:
        """Commande puis récupère le CSV ; `NoDataError` si la station est inconnue ou muette sur la période."""
        await self._reserve_command_slot()
        order = await self._get(
            "/commande-station/quotidienne",
            {
                "id-station": station,
                "date-deb-periode": f"{first.isoformat()}T00:00:00Z",
                "date-fin-periode": f"{last.isoformat()}T23:59:59Z",
            },
        )
        if order.status_code in (400, 404):  # 400 documenté ; 404 relevé le 18/11/2026 pour un numéro qui n'existe pas
            raise NoDataError(f"station {station} inconnue ou période refusée")
        if order.status_code != 202:
            raise UpstreamError(f"commande refusée ({order.status_code})")
        try:
            order_id = str(order.json()["elaboreProduitAvecDemandeResponse"]["return"])
        except (ValueError, KeyError, TypeError) as exc:
            raise UpstreamError("numéro de commande absent de la réponse") from exc
        for _ in range(MAX_POLLS):
            file = await self._get("/commande/fichier", {"id-cmde": order_id})
            if file.status_code == 201:
                return file.text
            if file.status_code == 204:
                await asyncio.sleep(POLL_EVERY_S)
                continue
            if file.status_code in (404, 410, 500):
                raise NoDataError(f"pas de fichier pour la station {station} ({file.status_code})")
            raise UpstreamError(f"fichier refusé ({file.status_code})")
        raise UpstreamError("commande pas prête après les interrogations prévues")

    async def daily(
        self, station: str, days: int, now: datetime | None = None
    ) -> tuple[list[StationDay], date, datetime | None]:
        """Les `days` derniers jours de la station (au plus `WINDOW_DAYS`), le dernier jour disponible et la date de l'interrogation du portail."""
        now = now or datetime.now(UTC)
        last = last_available_day(now)
        key = f"dpclim:{station}"
        cached = self._cache.get_cache(key)
        if cached is not None and cached[0].get("last") == last.isoformat():
            return self._slice(cached[0], days), last, datetime.fromtimestamp(cached[1], tz=UTC)
        lock = self._locks.setdefault(station, asyncio.Lock())
        async with lock:
            cached = self._cache.get_cache(key)  # une autre requête vient peut-être de le remplir
            if cached is not None and cached[0].get("last") == last.isoformat():
                return self._slice(cached[0], days), last, datetime.fromtimestamp(cached[1], tz=UTC)
            if self._cache.is_circuit_open(PROVIDER):
                return self._stale(cached, days, last, UpstreamError("circuit ouvert"))
            try:
                text = await self._download(station, last - timedelta(days=WINDOW_DAYS - 1), last)
                rows = parse_daily_csv(text)
            except NoDataError as exc:
                log.info("DPClim: %s", exc)
                rows = []
            except BusyError:
                raise
            except (httpx.HTTPError, UpstreamError) as exc:
                message = redact_secret(str(exc) or type(exc).__name__, self._settings)
                self.last_error = message
                self._cache.record_failure(
                    PROVIDER, message, self._settings.circuit_fail_threshold, self._settings.circuit_cooldown_seconds
                )
                return self._stale(cached, days, last, exc)
            payload = {"last": last.isoformat(), "days": [d.model_dump(mode="json") for d in rows]}
            stamp = self._cache.set_cache(key, PROVIDER, payload)
            self._cache.record_success(PROVIDER)
            self.last_error = None
            return self._slice(payload, days), last, datetime.fromtimestamp(stamp, tz=UTC)

    def _stale(
        self, cached: tuple[Any, float] | None, days: int, last: date, exc: Exception
    ) -> tuple[list[StationDay], date, datetime | None]:
        """Le portail a échoué : la dernière entrée connue si elle existe, sinon l'erreur."""
        if cached is not None and time.time() - cached[1] <= self._settings.max_stale_seconds:
            return self._slice(cached[0], days), last, datetime.fromtimestamp(cached[1], tz=UTC)
        raise UpstreamError(str(exc) or type(exc).__name__) from exc

    @staticmethod
    def _slice(payload: dict[str, Any], days: int) -> list[StationDay]:
        rows = [StationDay.model_validate(raw) for raw in payload.get("days", [])]
        return rows[-days:] if days < len(rows) else rows
