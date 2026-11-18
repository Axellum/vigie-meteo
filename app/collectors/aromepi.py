"""AROME-PI (prévision immédiate de Météo-France) : grêle, rafales, visibilité et type de précipitation, 6 h, pas de 15 min.

Un run par heure, publié environ 20 minutes après son heure (relevé le 18/11/2026 : le run de 09 h UTC était là à 09 h 19). Le WCS
`aromepi/1.0` ne rend **qu'une échéance par requête** (`time(a,b)` répond 404) : un run coûte 4 produits x 24 échéances = 96
requêtes de la carte de la France (3,5 Mo chacune), réduites aussitôt (`app/aromepi_grid.py`). Quota du portail : 50 requêtes par
minute pour toute la clé (PIAF et la mosaïque radar en prennent aussi) : les requêtes sont espacées (`METEO_AROMEPI_REQUESTS_PER_MINUTE`,
20 par défaut ; un run en ~5 min) et une échéance déjà lue n'est pas redemandée si le run est repris. Un run incomplet n'est jamais servi.

Les runs complets sont rangés sur disque (`aromepi/` à côté de la base) : deux processus servent l'API, un verrou de fichier
(`fcntl`) fait télécharger l'un seulement, l'autre lit le fichier qu'il écrit.

Produits lus (identifiants WCS relevés dans le `GetCapabilities` du 18/11/2026) :
- `WIND_SPEED_GUST_15MIN__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND___<run>_PT15M` (`height(10)`) : rafale maximale des 15 min, m/s.
- `DIAG_GRELE__GROUND_OR_WATER_SURFACE___<run>` : diagnostic de grêle, kg/m2, maximum de l'intervalle ; Météo-France : 8 à 16 = grésil
  (petite grêle), 16 et plus = grêle (code 10 du type de précipitation).
- `VISIBILITY_MINI_15MIN__GROUND_OR_WATER_SURFACE___<run>` : visibilité minimale des 15 min, m (plafonnée à 20 000).
- `PRECIPITATION_TYPE_15_MIN__GROUND_OR_WATER_SURFACE___<run>` : type de précipitation le plus grave des 15 min, code de la table
  4.201 de l'OMM et codes locaux 193 et 2xx (« intermittent ») de Météo-France.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from app.aromepi_grid import STEP_SECONDS, STEPS, AromePiRun, Geometry, pool_code, pool_max, pool_min
from app.collectors.grib2 import LatLonGrid, decode_latlon_simple
from app.config import Settings, redact_secret

try:  # pragma: no cover - dépend du système
    import fcntl
except ImportError:  # Windows : pas de verrou, un seul processus en développement
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SOURCE_MODEL = "meteofrance-arome-pi"
ATTRIBUTION = "Source : Météo-France, modèle AROME-PI (prévision immédiate, Licence Ouverte 2.0)"
# Zone demandée : la métropole et la Corse (celle de PIAF) ; au-delà de ces bornes la carte pleine contient un masque que le décodeur refuse.
BBOX = ("long(-6,10.5)", "lat(41,51.5)")
RETRY_EVERY_S = 180
FORMAT = "application/wmo-grib"


class RunIncompleteError(LookupError):
    """Une échéance du run n'est pas encore publiée."""


@dataclass(frozen=True)
class Product:
    key: str
    coverage: str  # `{run}` = date du run
    extra_subsets: tuple[str, ...]


PRODUCTS = (
    Product("gust", "WIND_SPEED_GUST_15MIN__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND___{run}_PT15M", ("height(10)",)),
    Product("hail", "DIAG_GRELE__GROUND_OR_WATER_SURFACE___{run}", ()),
    Product("visibility", "VISIBILITY_MINI_15MIN__GROUND_OR_WATER_SURFACE___{run}", ()),
    Product("ptype", "PRECIPITATION_TYPE_15_MIN__GROUND_OR_WATER_SURFACE___{run}", ()),
)


def run_stamp(run: datetime) -> str:
    return run.astimezone(UTC).strftime("%Y-%m-%dT%H.%M.%SZ")


def run_candidates(now: datetime, publish_delay_minutes: int, count: int = 2) -> list[datetime]:
    """Runs à essayer, du plus récent au plus ancien (un run par heure, publié en retard)."""
    ref = (now.astimezone(UTC) - timedelta(minutes=publish_delay_minutes)).replace(minute=0, second=0, microsecond=0)
    return [ref - timedelta(hours=i) for i in range(count)]


class AromePiStore:
    """Les runs complets sur disque (un `.npz` par run) et le dernier en mémoire."""

    def __init__(self, directory: str | Path, keep_runs: int = 3) -> None:
        self.directory = Path(directory)
        self.keep_runs = keep_runs
        self._memory: AromePiRun | None = None

    def _path(self, epoch: int) -> Path:
        return self.directory / f"{epoch}.npz"

    def epochs(self) -> list[int]:
        if not self.directory.is_dir():
            return []
        found = []
        for entry in self.directory.glob("*.npz"):
            with contextlib.suppress(ValueError):
                found.append(int(entry.stem))
        return sorted(found)

    def save(self, run: AromePiRun) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        epoch = int(run.run.timestamp())
        target = self._path(epoch)
        tmp = target.with_suffix(".tmp")
        g = run.geometry
        with open(tmp, "wb") as handle:
            np.savez_compressed(
                handle,
                geometry=np.array([g.lat0, g.lon0, g.dlat, g.dlon, g.nj, g.ni], dtype=np.float64),
                gust_kmh=run.gust_kmh,
                hail=run.hail,
                visibility_m=run.visibility_m,
                ptype=run.ptype,
            )
        os.replace(tmp, target)

    def load(self, epoch: int) -> AromePiRun | None:
        try:
            with np.load(self._path(epoch)) as data:
                lat0, lon0, dlat, dlon, nj, ni = (float(x) for x in data["geometry"])
                run = AromePiRun(
                    run=datetime.fromtimestamp(epoch, tz=UTC),
                    geometry=Geometry(lat0, lon0, dlat, dlon, int(nj), int(ni)),
                    gust_kmh=data["gust_kmh"],
                    hail=data["hail"],
                    visibility_m=data["visibility_m"],
                    ptype=data["ptype"],
                )
        except (FileNotFoundError, ValueError, KeyError, OSError):
            return None
        if run.gust_kmh.shape[0] != STEPS:
            return None
        return run

    def latest(self) -> AromePiRun | None:
        """Le run le plus récent du disque ; le même objet tant qu'aucun plus récent n'est écrit."""
        epochs = self.epochs()
        if not epochs:
            return None
        newest = int(self._memory.run.timestamp()) if self._memory is not None else None
        if newest == epochs[-1]:
            return self._memory
        for epoch in reversed(epochs):  # un fichier illisible : le précédent
            run = self.load(epoch)
            if run is not None:
                self._memory = run
                return run
        return None

    def purge(self) -> int:
        removed = 0
        for epoch in self.epochs()[: -self.keep_runs]:
            with contextlib.suppress(FileNotFoundError):
                self._path(epoch).unlink()
                removed += 1
        if self.directory.is_dir():
            for stale in self.directory.glob("*.tmp"):
                with contextlib.suppress(OSError):
                    if stale.stat().st_mtime < time.time() - 3600:
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


def decode_and_reduce(product: Product, content: bytes) -> tuple[Geometry, np.ndarray]:
    """Une carte GRIB2 décodée puis réduite : la carte entière (7 Mo, 60 Mo de temporaires) n'existe que le temps de cet appel."""
    grid = decode_latlon_simple(content)
    return Geometry.of(grid), reduce_step(product, grid)


def reduce_step(product: Product, grid: LatLonGrid) -> np.ndarray:
    """Une carte de 0,01° réduite à sa grille de 0,04° et rangée dans son type de stockage."""
    values = grid.values
    if product.key == "gust":
        return np.clip(np.rint(pool_max(values) * 3.6), 0, 255).astype(np.uint8)
    if product.key == "hail":
        return np.clip(pool_max(values), 0.0, 60000.0).astype(np.float16)
    if product.key == "visibility":
        return np.clip(np.rint(pool_min(values)), 0, 65535).astype(np.uint16)
    return np.clip(np.rint(pool_code(values)), 0, 255).astype(np.uint8)


class AromePiCollector:
    """Lit un run AROME-PI quand il est publié et le range sur disque."""

    def __init__(self, client: httpx.AsyncClient, store: AromePiStore, settings: Settings) -> None:
        self._client = client
        self.store = store
        self._settings = settings
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self._last_try = 0.0
        self._pace_lock = asyncio.Lock()
        self._next_at = 0.0
        # Les échéances déjà lues du run en cours de reprise (un run se télécharge en ~5 min, une panne au milieu ne coûte pas 96 requêtes).
        self._partial_run: datetime | None = None
        self._partial: dict[tuple[str, int], np.ndarray] = {}
        # Un seul décodage à la fois : chacun a besoin d'une soixantaine de Mo, et le conteneur de la passerelle est limité à 1 Go.
        self._decode_lock = asyncio.Lock()
        self._geometry: Geometry | None = None

    def enabled(self) -> bool:
        key = self._settings.meteofrance_api_key
        return self._settings.aromepi_enabled and key is not None and bool(key.get_secret_value().strip())

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._settings.meteofrance_api_key.get_secret_value().strip(),  # type: ignore[union-attr]
            "accept": "*/*",
            "User-Agent": self._settings.user_agent,
        }

    async def _paced(self, url: str, params: list[tuple[str, str]], timeout: float) -> httpx.Response:
        """Une requête, au plus `aromepi_requests_per_minute` par minute pour ce processus."""
        interval = 60.0 / self._settings.aromepi_requests_per_minute
        async with self._pace_lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + interval
        return await self._client.get(url, params=params, headers=self._headers(), timeout=timeout)

    async def _exists(self, run: datetime) -> bool:
        """Le run est-il listé ? (une petite requête `DescribeCoverage`, pas une carte)"""
        coverage = PRODUCTS[1].coverage.format(run=run_stamp(run))
        params = [("service", "WCS"), ("version", "2.0.1"), ("coverageid", coverage)]
        resp = await self._paced(self._settings.meteofrance_aromepi_url + "/DescribeCoverage", params, 20.0)
        if resp.status_code in (400, 404):
            return False
        resp.raise_for_status()
        return True

    async def _step(self, run: datetime, product: Product, k: int) -> tuple[Geometry, np.ndarray]:
        params = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", product.coverage.format(run=run_stamp(run))),
            ("format", FORMAT),
            ("subset", f"time({STEP_SECONDS * k})"),
            *[("subset", s) for s in product.extra_subsets],
            *[("subset", s) for s in BBOX],
        ]
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._paced(self._settings.meteofrance_aromepi_url + "/GetCoverage", params, 40.0)
                if resp.status_code in (400, 404):
                    # Un run se publie échéance après échéance (relevé le 18/11/2026) : une échéance absente n'est pas une panne, on
                    # reprendra plus tard sans insister.
                    raise RunIncompleteError(f"échéance {k} de {product.key} pas encore publiée (run {run_stamp(run)})")
                resp.raise_for_status()
                async with self._decode_lock:
                    return await asyncio.to_thread(decode_and_reduce, product, resp.content)
            except RunIncompleteError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                await asyncio.sleep(2.0 * (attempt + 1))
        assert last is not None
        raise last

    async def _download(self, run: datetime) -> AromePiRun:
        if self._partial_run != run:
            self._partial_run, self._partial, self._geometry = run, {}, None
        sem = asyncio.Semaphore(self._settings.aromepi_concurrency)

        async def one(product: Product, k: int) -> None:
            if (product.key, k) in self._partial:
                return
            async with sem:
                geometry, reduced = await self._step(run, product, k)
            if self._geometry is None:
                self._geometry = geometry
            elif geometry != self._geometry:
                raise ValueError(f"cartes AROME-PI de géométries différentes dans le run {run_stamp(run)}")
            self._partial[(product.key, k)] = reduced

        try:
            async with asyncio.TaskGroup() as group:  # la première échéance qui échoue arrête les autres : pas de requêtes perdues
                for product in PRODUCTS:
                    for k in range(1, STEPS + 1):
                        group.create_task(one(product, k))
        except ExceptionGroup as errors:
            raise errors.exceptions[0] from None
        assert self._geometry is not None

        def stack(key: str) -> np.ndarray:
            return np.stack([self._partial[(key, k)] for k in range(1, STEPS + 1)])

        result = AromePiRun(
            run=run,
            geometry=self._geometry,
            gust_kmh=stack("gust"),
            hail=stack("hail"),
            visibility_m=stack("visibility"),
            ptype=stack("ptype"),
        )
        self._partial_run, self._partial, self._geometry = None, {}, None
        return result

    def due(self, now: float) -> bool:
        if now - self._last_try < RETRY_EVERY_S:
            return False
        epochs = self.store.epochs()
        newest = run_candidates(datetime.fromtimestamp(now, tz=UTC), self._settings.aromepi_publish_delay_minutes, 1)[0]
        return not epochs or epochs[-1] < int(newest.timestamp())

    async def refresh(self, now: float | None = None) -> bool:
        """Télécharge le dernier run publié s'il est plus récent que le dernier gardé. Vrai si un run a été écrit."""
        now = time.time() if now is None else now
        if not self.enabled() or not self.due(now):
            return False
        self._last_try = now
        with _exclusive(self.store.directory) as mine:
            if not mine:
                return False
            try:
                return await self._fetch_new(now)
            except RunIncompleteError as exc:  # normal pendant la publication d'un run : on reprend dans 3 minutes
                self.last_error = str(exc)
                log.info("AROME-PI : %s", exc)
                return False
            except Exception as exc:  # une panne de la source ne doit jamais sortir de la tâche de fond
                self.last_error = redact_secret(f"{type(exc).__name__}: {exc}", self._settings)[:300]
                log.warning("AROME-PI : %s", self.last_error)
                return False

    async def _fetch_new(self, now: float) -> bool:
        epochs = self.store.epochs()
        latest = epochs[-1] if epochs else 0
        for run in run_candidates(datetime.fromtimestamp(now, tz=UTC), self._settings.aromepi_publish_delay_minutes):
            if int(run.timestamp()) <= latest:
                return False  # rien de plus récent que ce qu'on garde
            if not await self._exists(run):
                continue
            loaded = await self._download(run)
            self.store.save(loaded)
            self.store.purge()
            self.last_success_at = now
            self.last_error = None
            return True
        return False

    async def run_forever(self) -> None:
        """Tâche de fond : une vérification par minute, un téléchargement seulement quand un run est dû."""
        while True:
            await self.refresh()
            await asyncio.sleep(60)
