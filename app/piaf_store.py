"""Le run PIAF rangé sur disque, pour que les deux processus de la passerelle ne le téléchargent qu'une fois.

`uvicorn --workers 2` lance deux processus qui ont chacun leur carte PIAF en mémoire. Sans partage, chacun téléchargeait
le même run (12 échéances de 3,5 Mo, un quota de 50 requêtes par minute pour toute la clé). Ici, un verrou de fichier
(bloquant, entre processus) fait télécharger un seul des deux : il range le run dans `piaf/` à côté de la base, l'autre
le relit au lieu de le redemander.

Un fichier `.npz` compressé par run (les cartes sont presque toutes à zéro) : son nom est l'heure du run (secondes UNIX)
et sa date de modification dit quand le run a été vérifié pour la dernière fois (téléchargé, ou confirmé comme le plus
récent publié).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

import numpy as np

from app.collectors.grib2 import LatLonGrid

try:  # Linux (le conteneur)
    import fcntl
except ImportError:  # Windows (développement et tests)
    fcntl = None  # type: ignore[assignment]
    import msvcrt

log = logging.getLogger(__name__)

LOCK_POLL_SECONDS = 0.2


def _try_lock(handle: IO[bytes]) -> bool:
    """Prend le verrou exclusif du fichier sans attendre : faux si l'autre processus le tient."""
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _unlock(handle: IO[bytes]) -> None:
    with contextlib.suppress(OSError):
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class PiafStore:
    """Les runs PIAF sur disque (un `.npz` par run, les `keep_runs` derniers gardés)."""

    def __init__(self, directory: str | Path, keep_runs: int = 2, lock_timeout: float = 120.0) -> None:
        self.directory = Path(directory)
        self.keep_runs = keep_runs
        self.lock_timeout = lock_timeout

    def _path(self, epoch: int) -> Path:
        return self.directory / f"{epoch}.npz"

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[bool]:
        """Verrou entre processus, attendu sans bloquer la boucle. Vrai si pris ; faux si l'attente a dépassé `lock_timeout`.

        Dans ce cas on continue sans verrou : mieux vaut un téléchargement en double qu'une passerelle sans pluie.
        """
        try:
            await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)
            handle = await asyncio.to_thread(open, self.directory / ".lock", "ab")
        except OSError as exc:  # dossier impossible à créer : le partage est coupé, la passerelle continue seule
            log.warning("PIAF : dossier de partage inutilisable (%s), on continue sans", exc)
            yield False
            return
        got = False
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                got = await asyncio.to_thread(_try_lock, handle)
                if got or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(LOCK_POLL_SECONDS)
            if not got:
                log.warning("PIAF : verrou de téléchargement non obtenu en %.0f s, on continue sans", self.lock_timeout)
            yield got
        finally:
            if got:
                _unlock(handle)
            handle.close()

    def epochs(self) -> list[int]:
        if not self.directory.is_dir():
            return []
        found = []
        for entry in self.directory.glob("*.npz"):
            with contextlib.suppress(ValueError):
                found.append(int(entry.stem))
        return sorted(found)

    def newest(self) -> tuple[int, float] | None:
        """(heure du run, dernière vérification) du run le plus récent sur disque, ou None."""
        for epoch in reversed(self.epochs()):
            try:
                return epoch, self._path(epoch).stat().st_mtime
            except OSError:
                continue
        return None

    def save(self, run: datetime, steps: list[LatLonGrid]) -> float:
        """Range un run (écriture atomique) et rend sa date de modification, qui vaut « vérifié maintenant »."""
        self.directory.mkdir(parents=True, exist_ok=True)
        epoch = int(run.timestamp())
        target = self._path(epoch)
        tmp = target.with_suffix(".tmp")
        first = steps[0]
        arrays = {f"s{k}": grid.values for k, grid in enumerate(steps)}
        with open(tmp, "wb") as handle:
            np.savez_compressed(
                handle, geometry=np.array([first.lat0, first.lon0, first.dlat, first.dlon], dtype=np.float64), **arrays
            )
        os.replace(tmp, target)
        return target.stat().st_mtime

    def touch(self, epoch: int) -> float | None:
        """Dit aux autres processus que ce run est toujours le plus récent publié ; rend la date posée."""
        path = self._path(epoch)
        try:
            os.utime(path, None)
            return path.stat().st_mtime
        except OSError:
            return None

    def load(self, epoch: int, expected_steps: int) -> tuple[list[LatLonGrid], float] | None:
        """Les cartes d'un run et sa dernière vérification, ou None si le fichier manque, est illisible ou n'a pas `expected_steps` échéances."""
        path = self._path(epoch)
        try:
            checked = path.stat().st_mtime
            with np.load(path) as data:
                lat0, lon0, dlat, dlon = (float(x) for x in data["geometry"])
                steps = [LatLonGrid(data[f"s{k}"], lat0, lon0, dlat, dlon) for k in range(expected_steps)]
                if f"s{expected_steps}" in data.files:
                    return None
        except (FileNotFoundError, ValueError, KeyError, OSError, EOFError, zipfile.BadZipFile):
            return None
        return steps, checked

    def purge(self) -> int:
        removed = 0
        for epoch in self.epochs()[: -self.keep_runs]:
            with contextlib.suppress(OSError):
                self._path(epoch).unlink()
                removed += 1
        if self.directory.is_dir():
            for stale in self.directory.glob("*.tmp"):
                with contextlib.suppress(OSError):
                    if stale.stat().st_mtime < time.time() - 3600:
                        stale.unlink()
        return removed


def epoch_of(run: datetime) -> int:
    return int(run.astimezone(UTC).timestamp())
