"""AROME-OM (prévision numérique de Météo-France pour l'outre-mer) : lecture des paquets `SP1` et rangement des fenêtres servies.

`DPPaquetAROME-OM/v1` : un modèle par territoire (`AROME-OM-ANTIL`, `-GUYANE`, `-INDIEN`, `-NCALED`, `-POLYN`), une grille de 0,025°, quatre
runs par jour (00, 06, 12, 18 h UTC) de 49 échéances horaires. Le paquet `SP1` (« paramètres courants à la surface ») donne tout ce que Vigie
montre : vent à 10 m et rafales, température et humidité à 2 m, pression au niveau de la mer, nébulosité et pluie cumulée. Un fichier GRIB2
(4 à 12 Mo, CCSDS) par échéance et par territoire : 49 x 5 = 245 requêtes par run, espacées (`METEO_AROMEOM_REQUESTS_PER_MINUTE`, 10 par défaut)
pour laisser le quota de 50 par minute à PIAF, AROME-PI, au radar et aux ensembles.

Relevé le 18/11/2026 : le run de 00 h UTC est listé à partir de 06 h 20 UTC (première échéance) et complet vers 08 h (INDIEN, le plus gros) ;
l'échéance 0 n'a ni rafale, ni nébulosité, ni pluie ; la pluie (`0-1-52`) est un **cumul depuis le début du run** (processus statistique 1, durée = l'échéance),
la rafale (`0-2-22`) un maximum de l'heure précédente (processus 2, durée 1 h) ; la pluie horaire est donc la différence de deux échéances.

Une fenêtre est écrite quand son territoire est lu en entier ; un run n'est servi que quand tous les territoires le sont (`meta.json` écrit en dernier).
Reprise : les territoires déjà rangés du run en cours ne sont pas relus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

from app.aromeom_grid import (
    STEPS,
    TERRITORIES,
    VARIABLES,
    WINDOWS_BY_NAME,
    AromeOmStore,
    Crop,
    Territory,
    crop_of,
    windows_of,
)
from app.collectors.grib2_fields import FieldKey, decode_fields
from app.config import Settings, redact_secret

try:  # pragma: no cover - dépend du système
    import fcntl
except ImportError:  # Windows : pas de verrou, un seul processus en développement
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

SOURCE_MODEL = "meteofrance-arome-om"
ATTRIBUTION = "Source : Météo-France, modèle AROME Outre-mer, Licence Ouverte 2.0"
RETRY_EVERY_S = 180
GRID = "0.025"
PACKAGE = "SP1"

KEYS: dict[str, FieldKey] = {
    "t": (0, 0, 0, 103, 2),  # température à 2 m, K
    "rh": (0, 1, 1, 103, 2),  # humidité relative à 2 m, %
    "ff": (0, 2, 1, 103, 10),  # vitesse du vent à 10 m, m/s
    "dd": (0, 2, 0, 103, 10),  # direction du vent à 10 m, degrés
    "gust": (0, 2, 22, 103, 10),  # rafale maximale de l'heure, m/s
    "pmsl": (0, 3, 1, 101, 0),  # pression au niveau de la mer, Pa
    "cloud": (0, 6, 1, 1, 0),  # nébulosité totale, %
    "rain": (0, 1, 52, 1, 0),  # cumul de pluie depuis le début du run, kg/m2
}
OPTIONAL_AT_ZERO = {"gust", "cloud", "rain"}  # absents de l'échéance 0 (relevé le 18/11/2026)


class RunIncompleteError(LookupError):
    """Une échéance du run n'est pas encore publiée."""


def run_candidates(now: datetime, publish_delay_minutes: int, count: int = 2) -> list[datetime]:
    """Runs à essayer, du plus récent au plus ancien (00, 06, 12 et 18 h UTC, publiés avec ~7 h de retard)."""
    ref = now.astimezone(UTC) - timedelta(minutes=publish_delay_minutes)
    base = ref.replace(hour=ref.hour - ref.hour % 6, minute=0, second=0, microsecond=0)
    return [base - timedelta(hours=6 * i) for i in range(count)]


def run_stamp(run: datetime) -> str:
    return run.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_step(content: bytes, lead: int, territory: Territory) -> tuple[dict[str, np.ndarray], dict[str, Crop]]:
    """Un fichier `SP1` : pour chaque fenêtre du territoire, un tableau `[j, i, grandeur]` en float32 (unités de `VARIABLES`).

    La pluie est le **cumul** depuis le début du run (la différence entre échéances se fait après). Une grandeur absente de l'échéance 0
    vaut NaN ; absente ailleurs, ou d'une échéance autre que celle demandée, c'est une erreur."""
    found = {}
    wanted = set(KEYS.values())
    for field in decode_fields(content, wanted):
        if field.lead_hours != lead:
            raise ValueError(f"échéance {field.lead_hours} h dans le fichier de l'échéance {lead} h")
        found[field.key] = field.grid
    windows = windows_of(territory.key)
    crops: dict[str, Crop] = {}
    out: dict[str, np.ndarray] = {}
    for window in windows:
        planes: list[np.ndarray | None] = []
        crop = None
        for name in ("t", "rh", "ff", "dd", "gust", "pmsl", "cloud", "rain"):
            grid = found.get(KEYS[name])
            if grid is None:
                if lead == 0 and name in OPTIONAL_AT_ZERO:
                    planes.append(None)
                    continue
                raise ValueError(f"champ {name} absent de l'échéance {lead} h")
            this = crop_of(grid, window)
            if this is None:
                raise ValueError(f"la fenêtre {window.name} sort de la carte {territory.key}")
            if crop is not None and this != crop:
                raise ValueError("champs de grilles différentes dans un fichier")
            crop = this
            planes.append(grid.values[this.j0 : this.j1, this.i0 : this.i1])
        assert crop is not None
        shape = crop.shape
        stack = np.full((*shape, len(VARIABLES)), np.nan, dtype=np.float32)
        convert = (
            lambda v: v - 273.15,  # noqa: E731
            lambda v: v,
            lambda v: v * 3.6,
            lambda v: v,
            lambda v: v * 3.6,
            lambda v: v / 100.0 - 1000.0,
            lambda v: v,
            lambda v: v,
        )
        for k, plane in enumerate(planes):
            if plane is not None:
                stack[:, :, k] = convert[k](plane)
        out[window.name] = stack
        crops[window.name] = crop
    return out, crops


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


class AromeOmCollector:
    """Lit un run AROME-OM quand il est publié et range ses fenêtres sur disque."""

    def __init__(self, client: httpx.AsyncClient, store: AromeOmStore, settings: Settings) -> None:
        self._client = client
        self.store = store
        self._settings = settings
        self.last_success_at: float | None = None
        self.last_error: str | None = None
        self._last_try = 0.0
        self._next_at = 0.0
        self._pace_lock = asyncio.Lock()
        self._decode_lock = asyncio.Lock()

    def enabled(self) -> bool:
        key = self._settings.meteofrance_api_key
        return self._settings.aromeom_enabled and key is not None and bool(key.get_secret_value().strip())

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": self._settings.meteofrance_api_key.get_secret_value().strip(),  # type: ignore[union-attr]
            "accept": "*/*",
            "User-Agent": self._settings.user_agent,
        }

    def _url(self, territory: Territory, tail: str = "") -> str:
        return f"{self._settings.meteofrance_aromeom_url}/models/AROME-OM-{territory.key}/grids/{GRID}/packages/{PACKAGE}{tail}"

    async def _paced(self, url: str, params: dict[str, str], timeout: float) -> httpx.Response:
        interval = 60.0 / self._settings.aromeom_requests_per_minute
        async with self._pace_lock:
            wait = self._next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = time.monotonic() + interval
        return await self._client.get(url, params=params, headers=self._headers(), timeout=timeout)

    async def _listed(self, run: datetime) -> bool:
        """Le run est-il listé pour le premier territoire ? (une petite requête, pas un fichier)"""
        resp = await self._paced(self._url(TERRITORIES[0]), {"referencetime": run_stamp(run)}, 30.0)
        if resp.status_code in (400, 404):
            return False
        resp.raise_for_status()
        return any(link.get("time") == "000H" for link in resp.json().get("links", []))

    async def _fetch_step(self, run: datetime, territory: Territory, lead: int) -> bytes:
        params = {"referencetime": run_stamp(run), "time": f"{lead:03d}H", "format": "grib2", "grid": GRID, "package": PACKAGE}
        url = f"{self._settings.meteofrance_aromeom_url}/models/AROME-OM-{territory.key}/grids/{GRID}/packages/{PACKAGE}/product{territory.product}"
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._paced(url, params, 90.0)
                if resp.status_code in (400, 404):
                    raise RunIncompleteError(f"échéance {lead:03d}H de {territory.key} pas encore publiée (run {run_stamp(run)})")
                resp.raise_for_status()
                return resp.content
            except RunIncompleteError:
                raise
            except httpx.HTTPError as exc:
                last = exc
                await asyncio.sleep(2.0 * (attempt + 1))
        assert last is not None
        raise last

    async def _download_territory(self, run: datetime, territory: Territory, work: Path) -> dict[str, Crop]:
        """Les 49 échéances d'un territoire : une carte à la fois en mémoire, le tableau des fenêtres rangé à la fin."""
        arrays: dict[str, np.ndarray] = {}
        crops: dict[str, Crop] = {}
        previous_rain: dict[str, np.ndarray] = {}
        for lead in range(STEPS):
            content = await self._fetch_step(run, territory, lead)
            async with self._decode_lock:
                steps, step_crops = await asyncio.to_thread(extract_step, content, lead, territory)
            del content
            for name, stack in steps.items():
                if name not in arrays:
                    arrays[name] = np.full((STEPS, *stack.shape), np.nan, dtype=np.float16)
                    crops[name] = step_crops[name]
                elif step_crops[name] != crops[name]:
                    raise ValueError("géométrie différente d'une échéance à l'autre")
                rain = stack[:, :, VARIABLES.index("precip_mm")].copy()
                hourly = stack.copy()
                if lead > 0:
                    hourly[:, :, VARIABLES.index("precip_mm")] = np.maximum(rain - previous_rain[name], 0.0)
                previous_rain[name] = rain
                arrays[name][lead] = hourly
        work.mkdir(parents=True, exist_ok=True)
        for name, array in arrays.items():
            tmp = work / f"{name}.tmp.npy"
            np.save(tmp, array)
            tmp.replace(work / f"{name}.npy")
        return crops

    async def _download(self, run: datetime) -> None:
        epoch = int(run.timestamp())
        work = self.store.work_dir(epoch)
        crops: dict[str, Crop] = {}
        meta_path = work / "crops.json"
        if meta_path.is_file():
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
            crops = {name: Crop(**c) for name, c in saved.items() if (work / f"{name}.npy").is_file()}
        for territory in TERRITORIES:
            names = [w.name for w in windows_of(territory.key)]
            if all(n in crops for n in names):
                continue  # déjà lu avant une reprise
            got = await self._download_territory(run, territory, work)
            crops.update(got)
            meta_path.write_text(
                json.dumps({n: {"j0": c.j0, "j1": c.j1, "i0": c.i0, "i1": c.i1, "lat0": c.lat0, "lon0": c.lon0, "dlat": c.dlat, "dlon": c.dlon} for n, c in crops.items()}),
                encoding="utf-8",
            )
        if set(crops) != set(WINDOWS_BY_NAME):
            raise ValueError("fenêtres manquantes à la fin du run")
        self.store.finish(epoch, crops)

    def due(self, now: float) -> bool:
        if now - self._last_try < RETRY_EVERY_S:
            return False
        epochs = self.store.epochs()
        newest = run_candidates(datetime.fromtimestamp(now, tz=UTC), self._settings.aromeom_publish_delay_minutes, 1)[0]
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
            except RunIncompleteError as exc:
                self.last_error = str(exc)
                log.info("AROME-OM : %s", exc)
                return False
            except Exception as exc:  # une panne de la source ne doit jamais sortir de la tâche de fond
                self.last_error = redact_secret(f"{type(exc).__name__}: {exc}", self._settings)[:300]
                log.warning("AROME-OM : %s", self.last_error)
                return False

    async def _fetch_new(self, now: float) -> bool:
        epochs = self.store.epochs()
        latest = epochs[-1] if epochs else 0
        for run in run_candidates(datetime.fromtimestamp(now, tz=UTC), self._settings.aromeom_publish_delay_minutes):
            if int(run.timestamp()) <= latest:
                return False
            if not await self._listed(run):
                continue
            await self._download(run)
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
