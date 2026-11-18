"""Météo des forêts (`DPMeteoForets/v1`) : le danger de feux de forêt par département, pour J+1 et J+2.

Le flux est **saisonnier** : publié chaque jour vers 17 h locales de juin à septembre (`API-Doc_MeteoDesForets`). Hors saison le portail répond 404
(`{"detail":"no matching blob"}`, relevé le 18/11/2026) : ce n'est pas une panne, la route répond `out_of_season` et la santé reste « fresh ».

Le fichier lu est `GET /carte/encours` : le CSV `tableau-meteo-foret.csv` de tous les départements. Son format vient de l'échantillon publié par
Météo-France sur data.gouv.fr (`DONNEES_EXPERTISEES_MeteoDesForets_echantillons.zip`, Licence Ouverte 2.0, fichier du 22/05/2024) : en-tête
`reference_time;dep_code;niveau_j1;niveau_j2;dep_nom`, séparateur `;`, niveaux de 1 (faible) à 4 (très élevé). Les archives annuelles du même jeu
(`mdf_2025.csv.gz`) ont un autre en-tête (`date,num_dep,...`, séparateur `,`) : l'analyse accepte les deux. **Le fichier servi aujourd'hui par le portail n'a
pas pu être lu hors saison** : si son format a changé depuis 2024, l'analyse ne trouve aucun département, le contrôle compte comme une erreur (santé « stale »
puis « unavailable », message dans `last_error`) au lieu de servir une carte vide.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import logging
import time
from datetime import datetime
from typing import Any

import httpx

from app.cache import CacheStore
from app.config import Settings, redact_secret
from app.models import ForestDepartment

log = logging.getLogger(__name__)

PROVIDER = "meteofrance-forests"
SOURCE_MODEL = "meteofrance-forests"
ATTRIBUTION = "Source : Météo-France, Météo des forêts, Licence Ouverte 2.0"
KEY = "forests:latest"
STALE_AFTER_S = 12 * 3600  # le dernier contrôle du portail n'est plus « frais » au-delà


def _level(raw: str | None) -> int | None:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return None
    return value if 1 <= value <= 4 else None


def parse_forests_csv(text: str) -> tuple[datetime | None, list[ForestDepartment]]:
    """La date de publication et les départements du CSV (séparateur `;` ou `,`, en-têtes de l'échantillon de 2024 ou des archives)."""
    text = text.lstrip("﻿")
    head = text.splitlines()[0] if text.strip() else ""
    delimiter = ";" if head.count(";") >= head.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    stamp: datetime | None = None
    departments: list[ForestDepartment] = []
    for row in reader:
        code = (row.get("dep_code") or row.get("num_dep") or "").strip()
        if not code:
            continue
        if code.isdigit():
            code = code.zfill(2)
        raw_time = (row.get("reference_time") or row.get("date") or "").strip()
        if stamp is None and raw_time:
            with contextlib.suppress(ValueError):
                stamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        departments.append(
            ForestDepartment(
                department=code,
                name=(row.get("dep_nom") or "").strip() or None,
                level_d1=_level(row.get("niveau_j1")),
                level_d2=_level(row.get("niveau_j2")),
            )
        )
    return stamp, departments


class ForestsCollector:
    """Lit la Météo des forêts : toutes les 30 minutes en saison, toutes les 6 heures hors saison."""

    def __init__(self, client: httpx.AsyncClient, cache: CacheStore, settings: Settings) -> None:
        self._client = client
        self._cache = cache
        self._settings = settings
        self.last_error: str | None = None

    def enabled(self) -> bool:
        key = self._settings.meteofrance_api_key
        return self._settings.forests_enabled and key is not None and bool(key.get_secret_value().strip())

    def latest(self) -> tuple[dict[str, Any], float] | None:
        """Le dernier contrôle : `({"state": "available"|"out_of_season", "reference_time": ..., "departments": [...]}, instant)`."""
        return self._cache.get_cache(KEY)

    async def check(self) -> None:
        """Interroge le portail une fois et range le résultat (404 = hors saison)."""
        headers = {
            "apikey": self._settings.meteofrance_api_key.get_secret_value().strip(),  # type: ignore[union-attr]
            "accept": "*/*",
            "User-Agent": self._settings.user_agent,
        }
        try:
            resp = await self._client.get(
                f"{self._settings.meteofrance_forests_url}/carte/encours",
                headers=headers,
                timeout=self._settings.http_timeout_seconds * 2,
            )
            if resp.status_code == 404:
                payload: dict[str, Any] = {"state": "out_of_season", "reference_time": None, "departments": []}
            else:
                resp.raise_for_status()
                stamp, departments = parse_forests_csv(resp.text)
                if not departments:
                    raise ValueError("aucun département lisible dans le fichier")
                payload = {
                    "state": "available",
                    "reference_time": stamp.isoformat() if stamp else None,
                    "departments": [d.model_dump(mode="json") for d in departments],
                }
        except (httpx.HTTPError, ValueError) as exc:
            message = redact_secret(str(exc) or type(exc).__name__, self._settings)
            self.last_error = message
            self._cache.record_failure(
                PROVIDER, message, self._settings.circuit_fail_threshold, self._settings.circuit_cooldown_seconds
            )
            log.warning("Météo des forêts: %s", message)
            return
        self._cache.set_cache(KEY, PROVIDER, payload)
        self._cache.record_success(PROVIDER)
        self.last_error = None

    async def run_forever(self) -> None:
        while True:
            await self.check()
            latest = self.latest()
            in_season = latest is not None and latest[0].get("state") == "available"
            minutes = (
                self._settings.forests_poll_minutes if in_season else self._settings.forests_off_season_poll_minutes
            )
            if self.last_error is not None:
                minutes = min(minutes, 15)
            await asyncio.sleep(minutes * 60)

    def health(self, now: float | None = None) -> tuple[str, float | None]:
        """`(statut, instant du dernier contrôle)` : « fresh » si le portail a répondu (en saison ou non) il y a moins de 12 h."""
        if not self.enabled():
            return "not_configured", None
        latest = self.latest()
        if latest is None:
            return "unavailable", None
        age = (now if now is not None else time.time()) - latest[1]
        return ("fresh" if age <= STALE_AFTER_S else "stale"), latest[1]
