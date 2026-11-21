"""Mosaïques radar Météo-France (lame d'eau : métropole, Antilles, Réunion, Nouvelle-Calédonie) : téléchargement toutes les 5 minutes et images gardées.

L'API `DPRadar` ne rend que la dernière image (pas d'historique) : la passerelle la lit dès qu'une nouvelle est
publiée (la date de fin des 5 minutes de cumul, `validity_time`, se lit dans un petit document JSON avant le fichier de
2 Mo), la range sur disque en classes d'intensité (`app/radar_grid.py`) et garde les dernières heures pour l'animation.
Relevé le 18/11/2026 : l'image qui finit à 08 h 25 était publiée à 08 h 27.

Deux processus servent l'API : un verrou de fichier (`fcntl`, absent sous Windows) évite qu'ils téléchargent chacun.
Quota de l'API : 850 requêtes / 5 min (DPRadar) ; ici deux requêtes toutes les 5 minutes et par zone (un collecteur par zone,
chacun avec son dossier : `radar/` pour la métropole, `radar/<zone>/` pour les autres).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import zlib
from collections import OrderedDict
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np

from app.config import Settings, redact_secret
from app.radar_grid import METROPOLE, RadarArea, RadarFrame, decode_hdf5

try:  # pragma: no cover - dépend du système
    import fcntl
except ImportError:  # Windows : pas de verrou, un seul processus en développement
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SOURCE_MODEL = "meteofrance-lame-d-eau"
ATTRIBUTION = "Source : Météo-France, mosaïque radar lame d'eau (Licence Ouverte 2.0)"
INTERVAL_S = 300
# Une image est publiée 2 à 3 minutes après sa fin : on ne la demande qu'une minute après le pas suivant.
FIRST_TRY_AFTER_S = INTERVAL_S + 60
RETRY_EVERY_S = 45
FRESH_WITHIN_S = 900


class RadarStore:
    """Les images sur disque (une par fin de cumul, en classes d'intensité compressées) et les dernières en mémoire."""

    def __init__(self, directory: str | Path, keep_seconds: int, memory_frames: int = 2, area: RadarArea = METROPOLE) -> None:
        self.directory = Path(directory)
        self.area = area
        self.keep_seconds = keep_seconds
        self._memory: OrderedDict[int, RadarFrame] = OrderedDict()
        self._memory_frames = memory_frames

    def _path(self, epoch: int) -> Path:
        return self.directory / f"{epoch}.rz"

    def epochs(self) -> list[int]:
        """Les fins de cumul gardées sur disque, de la plus ancienne à la plus récente."""
        if not self.directory.is_dir():
            return []
        found = []
        for entry in self.directory.glob("*.rz"):
            with contextlib.suppress(ValueError):
                found.append(int(entry.stem))
        return sorted(found)

    def save(self, frame: RadarFrame) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(frame.epoch)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(zlib.compress(frame.classes.tobytes(), 1))
        os.replace(tmp, target)

    def load(self, epoch: int) -> RadarFrame | None:
        if epoch in self._memory:
            self._memory.move_to_end(epoch)
            return self._memory[epoch]
        path = self._path(epoch)
        try:
            raw = zlib.decompress(path.read_bytes())
        except (FileNotFoundError, zlib.error):
            return None
        if len(raw) != self.area.rows * self.area.cols:
            return None
        frame = RadarFrame(
            end=datetime.fromtimestamp(epoch, tz=UTC),
            classes=np.frombuffer(raw, dtype=np.uint8).reshape(self.area.rows, self.area.cols),
            area=self.area,
        )
        self._memory[epoch] = frame
        while len(self._memory) > self._memory_frames:
            self._memory.popitem(last=False)
        return frame

    def purge(self, now: float) -> int:
        """Supprime les images plus vieilles que la durée gardée ; rend leur nombre. Les `.tmp` oubliés partent aussi."""
        removed = 0
        for epoch in self.epochs():
            if epoch < now - self.keep_seconds:
                with contextlib.suppress(FileNotFoundError):
                    self._path(epoch).unlink()
                    removed += 1
                self._memory.pop(epoch, None)
        if self.directory.is_dir():
            for stale in self.directory.glob("*.tmp"):
                with contextlib.suppress(OSError):
                    if stale.stat().st_mtime < now - 3600:
                        stale.unlink()
        return removed


@contextlib.contextmanager
def _exclusive(directory: Path) -> Iterator[bool]:
    """Verrou de fichier sans attente : vrai si ce processus l'a, faux si l'autre télécharge déjà."""
    if fcntl is None:
        yield True
        return
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse_validity(doc: object) -> int:
    """La fin de cumul (époque UTC) de l'image la plus récente, lue dans le document JSON de l'observation."""
    if not isinstance(doc, dict):
        raise ValueError("document d'observation illisible")
    for link in doc.get("links", []):
        stamp = link.get("validity_time") if isinstance(link, dict) else None
        if stamp:
            return int(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
    raise ValueError("aucune date de validité dans le document d'observation")


class RadarCollector:
    """Lit la mosaïque quand une nouvelle image est publiée."""

    def __init__(self, client: httpx.AsyncClient, store: RadarStore, settings: Settings, area: RadarArea = METROPOLE) -> None:
        self._client = client
        self.store = store
        self.area = area
        # L'adresse de la métropole est réglable (`METEOFRANCE_RADAR_URL`) ; les autres zones n'en changent que le nom de la zone.
        self._url = settings.radar_observation_url.replace(f"/mosaiques/{METROPOLE.dpradar}/", f"/mosaiques/{area.dpradar}/")
        self._settings = settings
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self._last_try = 0.0

    def enabled(self) -> bool:
        key = self._settings.meteofrance_api_key
        return self._settings.radar_enabled and key is not None and bool(key.get_secret_value().strip())

    def due(self, now: float) -> bool:
        """Une image plus récente peut-elle exister ? (rien d'écrit, ou le pas suivant est publié)"""
        epochs = self.store.epochs()
        if now - self._last_try < RETRY_EVERY_S:
            return False
        return not epochs or now >= epochs[-1] + FIRST_TRY_AFTER_S

    async def refresh(self, now: float | None = None) -> bool:
        """Télécharge l'image si elle est plus récente que la dernière gardée. Vrai si une image a été écrite."""
        now = time.time() if now is None else now
        if not self.enabled() or not self.due(now):
            return False
        self._last_try = now
        with _exclusive(self.store.directory) as mine:
            if not mine:
                return False
            try:
                return await self._fetch_new(now)
            except Exception as exc:  # une panne de la source ne doit jamais sortir de la tâche de fond
                self.last_error = redact_secret(f"{type(exc).__name__}: {exc}", self._settings)[:300]
                log.warning("radar %s : %s", self.area.slug, self.last_error)
                return False

    async def _fetch_new(self, now: float) -> bool:
        headers = {
            "apikey": self._settings.meteofrance_api_key.get_secret_value().strip(),  # type: ignore[union-attr]
            "accept": "*/*",
            "User-Agent": self._settings.user_agent,
        }
        timeout = self._settings.http_timeout_seconds
        doc = await self._client.get(self._url, headers=headers, timeout=timeout)
        doc.raise_for_status()
        newest = parse_validity(doc.json())
        epochs = self.store.epochs()
        if epochs and newest <= epochs[-1]:
            return False  # pas encore de nouvelle image : on redemandera dans 45 s
        resp = await self._client.get(
            f"{self._url}/produit", params={"maille": 500}, headers=headers, timeout=60.0
        )
        resp.raise_for_status()
        frame = decode_hdf5(resp.content, self.area)
        self.store.save(frame)
        self.store.purge(now)
        self.last_success_at = now
        self.last_error = None
        return True

    async def run_forever(self) -> None:
        """Tâche de fond : une vérification par minute, un téléchargement seulement quand une image est due."""
        while True:
            await self.refresh()
            await asyncio.sleep(60)
