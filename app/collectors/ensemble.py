"""Prévisions d'ensemble de Météo-France (PE-AROME et PE-ARPEGE) : probabilités de pluie, de neige, de rafales, de grêle et d'orage.

Relevé le 18/11/2026 avec la clé du projet :

**PE-AROME** (`public-api.meteofrance.fr/public/pearome/1.0/wcs/MF-NWP-HIGHRES-PEAROME-0025-FRANCE-WCS`) : 4 runs par jour (03, 09, 15, 21 h UTC),
échéances horaires de +1 h à +51 h, 0,025°, **les probabilités sont déjà calculées** par Météo-France (`N_PROBA_PRECI03_1` : « 3 hours liquid
precipitation more than 1mm probability », en %). Une échéance par requête (une plage `time(a,b)` répond 404), GRIB2 de 0,8 Mo pour la France.
On lit les fenêtres de 3 h qui se suivent (+3 h, +6 h … +51 h, 17 échéances) : pluie ≥ 1 mm et ≥ 5 mm, neige ≥ 1 mm, puis les probabilités
instantanées à la fin de chaque tranche : rafales (> 30 kt et > 50 kt, la description du produit dit « kt » : nœuds), grêle (diagnostic AROME
> 8 kg/m2), réflectivité maximale > 40 dBZ (orage). 7 produits x 17 échéances = 119 requêtes par run.

**PE-ARPEGE** (`pearpege/1.0`, 35 membres : un service `MF-NWP-GLOBAL-PEARP<000..034>-01-EUROPE-WCS` par membre, 0,1°, 4 runs par jour à 00, 06,
12, 18 h UTC) : pas de probabilités, des valeurs. Le cumul de pluie de 24 h (`TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE___<run>_P1D`, mm) est lu
pour chaque membre aux échéances +24 h, +48 h, +72 h et +96 h (140 requêtes de 34 Ko) et la probabilité de dépasser 1, 5, 10 et 20 mm est la part
des 35 membres qui les dépassent. **Piège** : le WCS répond de temps en temps `400 « Lat and long parameters are not allowed »` à une requête
valable (la même requête repasse une seconde plus tard) : ces 400 sont rejoués.

Quota du portail : 50 requêtes par minute pour toute la clé (PIAF, AROME-PI et le radar en prennent aussi) : chaque lecteur espace ses requêtes,
et un seul processus télécharge (verrou de fichier). Les cartes sont réduites aussitôt (`app/ensemble_grid.py`) : un run tient en quelques Mo.
Un run incomplet n'est jamais servi.

Chaque carte déjà lue d'un run en cours est gardée sur disque (`partial/<run>/`, un `.npy` de 70 Ko par carte réduite) : un
redémarrage de la passerelle reprend où il en était au lieu de redemander les 119 (ou 140) cartes. Le 25/11/2026, deux
redémarrages en pleine panne de PE-AROME (des 429 de leur serveur de données pendant des heures) avaient perdu ce qui était lu.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from app.collectors.grib2 import LatLonGrid, decode_latlon_simple
from app.config import Settings, redact_secret
from app.ensemble_grid import GridGeometry, ProbabilityRun, crop_to_box, pool_mean, to_percent

try:  # pragma: no cover - dépend du système
    import fcntl
except ImportError:  # Windows : pas de verrou, un seul processus en développement
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

AROME_SOURCE_MODEL = "meteofrance-pe-arome"
AROME_ATTRIBUTION = "Source : Météo-France, prévision d'ensemble PE-AROME"
ARPEGE_SOURCE_MODEL = "meteofrance-pe-arpege"
ARPEGE_ATTRIBUTION = "Source : Météo-France, prévision d'ensemble PE-ARPEGE"
BBOX = ("long(-6,10.5)", "lat(41,51.5)")  # la métropole et la Corse, comme PIAF et AROME-PI
FORMAT = "application/wmo-grib"
RETRY_EVERY_S = 180
AROME_POOL = 2
MEMBERS = 35  # 000 (contrôle) à 034
# Le 400 intermittent du WCS d'ensemble (relevé le 18/11/2026) : la même requête repasse la fois suivante.
FLAKY_400 = re.compile(r"Lat and long parameters are not allowed", re.IGNORECASE)
FLAKY_RETRIES = 8


class RunIncompleteError(LookupError):
    """Une échéance du run n'est pas encore publiée."""


def run_stamp(run: datetime) -> str:
    return run.astimezone(UTC).strftime("%Y-%m-%dT%H.%M.%SZ")


def run_candidates(now: datetime, publish_delay_minutes: int, offset_hours: int, count: int = 2) -> list[datetime]:
    """Runs à essayer, du plus récent au plus ancien : un run toutes les 6 h, à `offset_hours` + 6 k (03/09/15/21 ou 00/06/12/18 h UTC)."""
    ref = (now.astimezone(UTC) - timedelta(minutes=publish_delay_minutes)).replace(minute=0, second=0, microsecond=0)
    while (ref.hour - offset_hours) % 6 != 0:
        ref -= timedelta(hours=1)
    return [ref - timedelta(hours=6 * i) for i in range(count)]


# ------------------------------------------------------------------------------------------------ les produits de PE-AROME


@dataclass(frozen=True)
class Product:
    key: str
    coverage: str  # `{run}` = date du run
    hours: tuple[int, ...]


THREE_HOURLY = tuple(range(3, 52, 3))  # 17 tranches de 3 h, jusqu'à +51 h
AROME_PRODUCTS = (
    Product("rain_1mm", "N_PROBA_PRECI03_1__GROUND_OR_WATER_SURFACE___{run}", THREE_HOURLY),
    Product("rain_5mm", "N_PROBA_PRECI03_5__GROUND_OR_WATER_SURFACE___{run}", THREE_HOURLY),
    Product("snow_1mm", "N_PROBA_SOLID03_1__GROUND_OR_WATER_SURFACE___{run}", THREE_HOURLY),
    Product("gust_30kt", "N_PROBA_RAF_30__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND___{run}", THREE_HOURLY),
    Product("gust_50kt", "N_PROBA_RAF_50__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND___{run}", THREE_HOURLY),
    Product("hail", "N_PROBA_D_GRELE_8__GROUND_OR_WATER_SURFACE___{run}", THREE_HOURLY),
    Product("storm", "N_PROBA_RFX_40DBZ__GROUND_OR_WATER_SURFACE___{run}", THREE_HOURLY),
)

# ------------------------------------------------------------------------------------------------ PE-ARPEGE
ARPEGE_HOURS = (24, 48, 72, 96)
ARPEGE_THRESHOLDS_MM = {"rain_1mm": 1.0, "rain_5mm": 5.0, "rain_10mm": 10.0, "rain_20mm": 20.0}
ARPEGE_COVERAGE = "TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE___{run}_P1D"
PARTIAL_MAX_AGE_S = 2 * 86400  # les cartes d'un run jamais fini : jetées après 2 jours


def geometry_to_array(g: GridGeometry) -> np.ndarray:
    return np.array([g.lat0, g.lon0, g.dlat, g.dlon, g.nj, g.ni, g.pool], dtype=np.float64)


def geometry_from_array(values: np.ndarray) -> GridGeometry:
    lat0, lon0, dlat, dlon, nj, ni, pool = (float(x) for x in values)
    return GridGeometry(lat0, lon0, dlat, dlon, int(nj), int(ni), int(pool))


def pooled_shape(g: GridGeometry) -> tuple[int, int]:
    """La forme d'une carte réduite par `pool_mean` (bords prolongés : arrondi au-dessus)."""
    return (-(-g.nj // g.pool), -(-g.ni // g.pool))


class ProbabilityStore:
    """Les runs complets sur disque (un `.npz` par run) et le dernier en mémoire ; les cartes d'un run en cours dans `partial/<run>/`."""

    def __init__(self, directory: str | Path, keep_runs: int = 3) -> None:
        self.directory = Path(directory)
        self.keep_runs = keep_runs
        self._memory: ProbabilityRun | None = None

    def _path(self, epoch: int) -> Path:
        return self.directory / f"{epoch}.npz"

    def _partial_dir(self, epoch: int) -> Path:
        return self.directory / "partial" / str(epoch)

    def save_piece(self, epoch: int, name: str, array: np.ndarray) -> None:
        """Une carte déjà lue du run `epoch` (ou sa géométrie), écrite d'un coup : un redémarrage la retrouve."""
        folder = self._partial_dir(epoch)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f"{name}.tmp"
        with open(tmp, "wb") as handle:
            np.save(handle, array, allow_pickle=False)
        os.replace(tmp, folder / f"{name}.npy")

    def load_pieces(self, epoch: int) -> dict[str, np.ndarray]:
        """Les cartes déjà lues du run `epoch` ; un fichier illisible est jeté (la carte sera redemandée)."""
        folder = self._partial_dir(epoch)
        pieces: dict[str, np.ndarray] = {}
        if not folder.is_dir():
            return pieces
        for entry in folder.glob("*.npy"):
            try:
                pieces[entry.stem] = np.load(entry, allow_pickle=False)
            except (ValueError, OSError, EOFError):
                with contextlib.suppress(OSError):
                    entry.unlink()
        return pieces

    def drop_pieces(self, epoch: int) -> None:
        shutil.rmtree(self._partial_dir(epoch), ignore_errors=True)

    def epochs(self) -> list[int]:
        if not self.directory.is_dir():
            return []
        found = []
        for entry in self.directory.glob("*.npz"):
            with contextlib.suppress(ValueError):
                found.append(int(entry.stem))
        return sorted(found)

    def save(self, run: ProbabilityRun) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        epoch = int(run.run.timestamp())
        target = self._path(epoch)
        tmp = target.with_suffix(".tmp")
        arrays: dict[str, np.ndarray] = {"geometry": geometry_to_array(run.geometry)}
        for key, layer in run.layers.items():
            arrays[f"L_{key}"] = layer
            arrays[f"H_{key}"] = np.array(run.hours[key], dtype=np.int32)
        with open(tmp, "wb") as handle:
            np.savez_compressed(handle, **arrays)
        os.replace(tmp, target)

    def load(self, epoch: int) -> ProbabilityRun | None:
        try:
            with np.load(self._path(epoch)) as data:
                geometry = geometry_from_array(data["geometry"])
                layers = {name[2:]: data[name] for name in data.files if name.startswith("L_")}
                hours = {key: tuple(int(h) for h in data[f"H_{key}"]) for key in layers}
        except (FileNotFoundError, ValueError, KeyError, OSError):
            return None
        if not layers or any(layer.shape[0] != len(hours[key]) for key, layer in layers.items()):
            return None
        return ProbabilityRun(datetime.fromtimestamp(epoch, tz=UTC), geometry, hours, layers)

    def latest(self) -> ProbabilityRun | None:
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
        epochs = self.epochs()
        newest = epochs[-1] if epochs else 0
        partial = self.directory / "partial"
        if partial.is_dir():  # les cartes d'un run déjà gardé, ou d'un run jamais fini depuis 2 jours
            for folder in partial.iterdir():
                with contextlib.suppress(OSError, ValueError):
                    if int(folder.name) <= newest or folder.stat().st_mtime < time.time() - PARTIAL_MAX_AGE_S:
                        shutil.rmtree(folder, ignore_errors=True)
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


class _EnsembleCollector:
    """Ce que les deux lecteurs partagent : requêtes espacées, 400 intermittents rejoués, décodage un à la fois, tâche de fond."""

    offset_hours = 0
    name = ""

    def __init__(self, client: httpx.AsyncClient, store: ProbabilityStore, settings: Settings, per_minute: int, delay_minutes: int) -> None:
        self._client = client
        self.store = store
        self._settings = settings
        self._per_minute = per_minute
        self._delay_minutes = delay_minutes
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self._last_try = 0.0
        self._pace_lock = asyncio.Lock()
        self._next_at = 0.0
        # Un seul décodage à la fois : une carte de PE-AROME décodée prend une dizaine de Mo, et le conteneur est limité à 1 Go.
        self._decode_lock = asyncio.Lock()

    def enabled(self) -> bool:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._settings.meteofrance_api_key.get_secret_value().strip(),  # type: ignore[union-attr]
            "accept": "*/*",
            "User-Agent": self._settings.user_agent,
        }

    def _key_ok(self) -> bool:
        key = self._settings.meteofrance_api_key
        return key is not None and bool(key.get_secret_value().strip())

    async def _paced(self, url: str, params: list[tuple[str, str]], timeout: float) -> httpx.Response:
        """Une requête, au plus `per_minute` par minute pour ce lecteur."""
        interval = 60.0 / self._per_minute
        async with self._pace_lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + interval
        return await self._client.get(url, params=params, headers=self._headers(), timeout=timeout)

    async def _grid(self, service_url: str, coverage: str, seconds: int, bbox: bool = True) -> LatLonGrid:
        """La carte d'une échéance (`seconds` après le run), décodée. `RunIncompleteError` quand elle n'est pas publiée.

        `bbox=False` : toute la grille du service, sans `subset` de latitude ni de longitude (voir `PeArpegeCollector`)."""
        params = [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", coverage),
            ("format", FORMAT),
            ("subset", f"time({seconds})"),
            *([("subset", s) for s in BBOX] if bbox else []),
        ]
        last: Exception | None = None
        flaky = 0
        for attempt in range(FLAKY_RETRIES + 3):
            try:
                resp = await self._paced(service_url + "/GetCoverage", params, 40.0)
                if resp.status_code == 400 and FLAKY_400.search(resp.text) and flaky < FLAKY_RETRIES:
                    flaky += 1
                    continue
                if resp.status_code in (400, 404):
                    raise RunIncompleteError(f"échéance +{seconds // 3600} h de {coverage[:40]} pas encore publiée")
                resp.raise_for_status()
                async with self._decode_lock:
                    return await asyncio.to_thread(decode_latlon_simple, resp.content)
            except RunIncompleteError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                await asyncio.sleep(2.0 * (attempt + 1))
        assert last is not None
        raise last

    async def _exists(self, service_url: str, coverage: str) -> bool:
        """Le run est-il listé ? (une petite requête `DescribeCoverage`, pas une carte)"""
        params = [("service", "WCS"), ("version", "2.0.1"), ("coverageid", coverage)]
        for _ in range(FLAKY_RETRIES):
            resp = await self._paced(service_url + "/DescribeCoverage", params, 20.0)
            if resp.status_code == 400 and FLAKY_400.search(resp.text):
                continue
            if resp.status_code in (400, 404):
                return False
            resp.raise_for_status()
            return True
        return False

    def due(self, now: float) -> bool:
        if now - self._last_try < RETRY_EVERY_S:
            return False
        epochs = self.store.epochs()
        newest = run_candidates(datetime.fromtimestamp(now, tz=UTC), self._delay_minutes, self.offset_hours, 1)[0]
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
                log.info("%s : %s", self.name, exc)
                return False
            except Exception as exc:  # une panne de la source ne doit jamais sortir de la tâche de fond
                self.last_error = redact_secret(f"{type(exc).__name__}: {exc}", self._settings)[:300]
                log.warning("%s : %s", self.name, self.last_error)
                return False

    async def _fetch_new(self, now: float) -> bool:
        epochs = self.store.epochs()
        latest = epochs[-1] if epochs else 0
        for run in run_candidates(datetime.fromtimestamp(now, tz=UTC), self._delay_minutes, self.offset_hours):
            if int(run.timestamp()) <= latest:
                return False  # rien de plus récent que ce qu'on garde
            if not await self._run_listed(run):
                continue
            loaded = await self._download(run)
            self.store.save(loaded)
            self.store.drop_pieces(int(run.timestamp()))
            self.store.purge()
            self.last_success_at = now
            self.last_error = None
            return True
        return False

    async def _run_listed(self, run: datetime) -> bool:
        raise NotImplementedError

    async def _download(self, run: datetime) -> ProbabilityRun:
        raise NotImplementedError

    def _forget(self, epoch: int) -> None:
        """Des cartes qui ne collent pas entre elles : on oublie tout le run en cours, le prochain essai repart de zéro."""
        self._partial_run = None
        self.store.drop_pieces(epoch)

    async def run_forever(self) -> None:
        """Tâche de fond : une vérification par minute, un téléchargement seulement quand un run est dû."""
        while True:
            await self.refresh()
            await asyncio.sleep(60)


class PeAromeCollector(_EnsembleCollector):
    """Lit un run PE-AROME (probabilités toutes faites) quand il est publié et le range sur disque."""

    offset_hours = 3
    name = "PE-AROME"

    def __init__(self, client: httpx.AsyncClient, store: ProbabilityStore, settings: Settings) -> None:
        super().__init__(client, store, settings, settings.pearome_requests_per_minute, settings.pearome_publish_delay_minutes)
        self._partial_run: datetime | None = None
        self._partial: dict[tuple[str, int], np.ndarray] = {}
        self._geometry: GridGeometry | None = None

    def enabled(self) -> bool:
        return self._settings.pearome_enabled and self._key_ok()

    async def _run_listed(self, run: datetime) -> bool:
        return await self._exists(self._settings.meteofrance_pearome_url, AROME_PRODUCTS[0].coverage.format(run=run_stamp(run)))

    def _resume(self, run: datetime) -> None:
        """Reprend les cartes de `run` déjà lues avant un redémarrage (`partial/<run>/`) ; rien si elles ne collent pas."""
        pieces = self.store.load_pieces(int(run.timestamp()))
        if "geometry" not in pieces:
            return
        geometry = geometry_from_array(pieces.pop("geometry"))
        wanted = {f"{p.key}_{h}": (p.key, h) for p in AROME_PRODUCTS for h in p.hours}
        self._geometry = geometry
        for name, values in pieces.items():
            if name in wanted and values.shape == pooled_shape(geometry) and values.dtype == np.uint8:
                self._partial[wanted[name]] = values
        if self._partial:
            log.info("%s : %d cartes du run %s reprises du disque", self.name, len(self._partial), run_stamp(run))

    async def _download(self, run: datetime) -> ProbabilityRun:
        epoch = int(run.timestamp())
        if self._partial_run != run:
            self._partial_run, self._partial, self._geometry = run, {}, None
            self._resume(run)
        sem = asyncio.Semaphore(self._settings.pearome_concurrency)

        async def one(product: Product, hour: int) -> None:
            if (product.key, hour) in self._partial:
                return
            async with sem:
                grid = await self._grid(self._settings.meteofrance_pearome_url, product.coverage.format(run=run_stamp(run)), hour * 3600)
            geometry = GridGeometry.of(grid, AROME_POOL)
            if self._geometry is None:
                self._geometry = geometry
                self.store.save_piece(epoch, "geometry", geometry_to_array(geometry))
            elif geometry != self._geometry:
                self._forget(epoch)
                raise ValueError(f"cartes PE-AROME de géométries différentes dans le run {run_stamp(run)}")
            values = to_percent(pool_mean(grid.values, AROME_POOL))
            self.store.save_piece(epoch, f"{product.key}_{hour}", values)
            self._partial[(product.key, hour)] = values

        try:
            async with asyncio.TaskGroup() as group:  # la première échéance qui échoue arrête les autres : pas de requêtes perdues
                for product in AROME_PRODUCTS:
                    for hour in product.hours:
                        group.create_task(one(product, hour))
        except ExceptionGroup as errors:
            raise errors.exceptions[0] from None
        assert self._geometry is not None
        layers = {p.key: np.stack([self._partial[(p.key, h)] for h in p.hours]) for p in AROME_PRODUCTS}
        result = ProbabilityRun(run, self._geometry, {p.key: p.hours for p in AROME_PRODUCTS}, layers)
        self._partial_run, self._partial, self._geometry = None, {}, None
        return result


class PeArpegeCollector(_EnsembleCollector):
    """Lit les 35 membres PE-ARPEGE d'un run, compte pour chaque seuil ceux qui le dépassent, et range les probabilités."""

    offset_hours = 0
    name = "PE-ARPEGE"

    def __init__(self, client: httpx.AsyncClient, store: ProbabilityStore, settings: Settings) -> None:
        super().__init__(client, store, settings, settings.pearp_requests_per_minute, settings.pearp_publish_delay_minutes)
        self._partial_run: datetime | None = None
        self._counts: dict[int, np.ndarray] = {}  # heure -> `[seuil, J, I]` : membres au-dessus du seuil
        self._done: set[tuple[int, int]] = set()
        self._geometry: GridGeometry | None = None

    def enabled(self) -> bool:
        return self._settings.pearp_enabled and self._key_ok()

    def _member_url(self, member: int) -> str:
        return self._settings.meteofrance_pearp_url.replace("{member}", f"{member:03d}")

    async def _run_listed(self, run: datetime) -> bool:
        return await self._exists(self._member_url(0), ARPEGE_COVERAGE.format(run=run_stamp(run)))

    def _add(self, hour: int, member: int, above: np.ndarray) -> None:
        """Compte un membre (`above[seuil]` = 1 là où il dépasse le seuil) ; pas d'attente au milieu : jamais compté deux fois."""
        counts = self._counts.setdefault(hour, np.zeros(above.shape, dtype=np.uint8))
        counts += above
        self._done.add((hour, member))

    def _resume(self, run: datetime) -> None:
        """Reprend les membres de `run` déjà lus avant un redémarrage (`partial/<run>/`) ; rien si ils ne collent pas."""
        pieces = self.store.load_pieces(int(run.timestamp()))
        if "geometry" not in pieces:
            return
        geometry = geometry_from_array(pieces.pop("geometry"))
        shape = (len(ARPEGE_THRESHOLDS_MM), geometry.nj, geometry.ni)
        wanted = {f"h{h}_m{m:03d}": (h, m) for h in ARPEGE_HOURS for m in range(MEMBERS)}
        self._geometry = geometry
        for name, above in pieces.items():
            if name in wanted and above.shape == shape and above.dtype == np.uint8:
                self._add(*wanted[name], above)
        if self._done:
            log.info("%s : %d cartes du run %s reprises du disque", self.name, len(self._done), run_stamp(run))

    async def _download(self, run: datetime) -> ProbabilityRun:
        epoch = int(run.timestamp())
        if self._partial_run != run:
            self._partial_run, self._counts, self._done, self._geometry = run, {}, set(), None
            self._resume(run)
        sem = asyncio.Semaphore(self._settings.pearp_concurrency)
        thresholds = tuple(ARPEGE_THRESHOLDS_MM.values())

        async def one(member: int, hour: int) -> None:
            if (hour, member) in self._done:
                return
            async with sem:
                # Toute l'Europe (772 Ko) puis découpe ici : avec un `subset` de latitude et de longitude, le service répond
                # « Lat and long parameters are not allowed » deux fois sur trois (mesuré le 18/11, jusqu'à 7 de suite), sans jamais
                # le faire sans emprise (20 sur 20).
                full = await self._grid(self._member_url(member), ARPEGE_COVERAGE.format(run=run_stamp(run)), hour * 3600, bbox=False)
            grid = crop_to_box(full, 41.0, -6.0, 51.5, 10.5)
            geometry = GridGeometry.of(grid, 1)
            if self._geometry is None:
                self._geometry = geometry
                self.store.save_piece(epoch, "geometry", geometry_to_array(geometry))
            elif geometry != self._geometry:
                self._forget(epoch)
                raise ValueError(f"cartes PE-ARPEGE de géométries différentes dans le run {run_stamp(run)}")
            above = np.stack([grid.values >= threshold for threshold in thresholds]).astype(np.uint8)
            self.store.save_piece(epoch, f"h{hour}_m{member:03d}", above)
            self._add(hour, member, above)

        try:
            async with asyncio.TaskGroup() as group:
                for hour in ARPEGE_HOURS:
                    for member in range(MEMBERS):
                        group.create_task(one(member, hour))
        except ExceptionGroup as errors:
            raise errors.exceptions[0] from None
        assert self._geometry is not None
        layers = {
            key: np.stack([to_percent(100.0 * self._counts[h][t] / MEMBERS) for h in ARPEGE_HOURS])
            for t, key in enumerate(ARPEGE_THRESHOLDS_MM)
        }
        result = ProbabilityRun(run, self._geometry, {key: ARPEGE_HOURS for key in layers}, layers)
        self._partial_run, self._counts, self._done, self._geometry = None, {}, set(), None
        return result
