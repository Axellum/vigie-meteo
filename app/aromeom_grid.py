"""AROME-OM (Météo-France, Outre-mer) : les fenêtres servies, leur série horaire par run et la lecture d'un point.

Les paquets du PaquetAROME-OM sont des cartes entières de chaque territoire (de 130 000 à 1,25 million de nœuds à 0,025°, 49 échéances
horaires de +0 h à +48 h, quatre runs par jour). Les ranger entières coûterait des centaines de Mo : on garde seulement des **fenêtres**
(rectangles de latitude et de longitude) autour des îles et du littoral habité. Un point hors de toutes les fenêtres n'est pas servi
(404) et le client garde sa météo habituelle. Les fenêtres sont dessinées à la main (marge d'environ 0,1° autour de la côte) :
elles n'ont pas été comparées à un masque terre-mer du modèle.

Chaque fenêtre garde, pour chaque échéance (le champ valable à `run + échéance`), huit grandeurs (`VARIABLES`) : instantanées
(température, humidité, vent moyen et direction, pression au niveau de la mer, nébulosité) ou sur l'heure qui finit à l'échéance
(rafale maximale, pluie, qui est la différence de deux cumuls depuis le début du run). L'échéance 0 n'a ni rafale ni pluie (NaN).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from app.collectors.grib2 import LatLonGrid

STEPS = 49  # +0 h à +48 h
VARIABLES = ("temperature_c", "humidity_pct", "wind_kmh", "wind_direction_deg", "gust_kmh", "pressure_hpa", "cloud_cover_pct", "precip_mm")
PRESSURE_OFFSET_HPA = 1000.0  # la pression est rangée moins 1000 hPa : un float16 y a un pas de 0,008 hPa (1,0 hPa à 1 000)


@dataclass(frozen=True)
class Territory:
    """Un modèle AROME-OM : son nom dans l'URL et le suffixe de son produit."""

    key: str  # `ANTIL`, `GUYANE`, `INDIEN`, `NCALED`, `POLYN`
    product: str  # `OMAN`, `OMGU`, `OMOI`, `OMNC`, `OMPF`


@dataclass(frozen=True)
class Window:
    """Un rectangle servi : `lat` et `lon` en degrés (lon de -180 à 180), le décalage horaire fixe du territoire (sans heure d'été)."""

    name: str
    territory: str
    lat: tuple[float, float]
    lon: tuple[float, float]
    utc_offset_s: int

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat[0] <= lat <= self.lat[1] and self.lon[0] <= lon <= self.lon[1]


TERRITORIES = (
    Territory("ANTIL", "OMAN"),
    Territory("GUYANE", "OMGU"),
    Territory("INDIEN", "OMOI"),
    Territory("NCALED", "OMNC"),
    Territory("POLYN", "OMPF"),
)

H = 3600
WINDOWS = (
    Window("guadeloupe-martinique", "ANTIL", (14.3, 16.6), (-62.1, -60.6), -4 * H),
    Window("saint-martin-saint-barthelemy", "ANTIL", (17.8, 18.2), (-63.2, -62.7), -4 * H),
    Window("guyane", "GUYANE", (2.0, 6.0), (-54.8, -51.5), -3 * H),
    Window("reunion", "INDIEN", (-21.5, -20.8), (55.1, 55.95), 4 * H),
    Window("mayotte", "INDIEN", (-13.1, -12.5), (44.9, 45.4), 3 * H),
    Window("nouvelle-caledonie", "NCALED", (-23.2, -19.4), (163.4, 169.3), 11 * H),
    Window("polynesie-societe", "POLYN", (-18.3, -16.2), (-152.2, -148.9), -10 * H),
    Window("polynesie-tuamotu-ouest", "POLYN", (-16.5, -14.3), (-148.6, -145.4), -10 * H),
    Window("polynesie-australes", "POLYN", (-24.2, -22.2), (-151.6, -147.3), -10 * H),
)
WINDOWS_BY_NAME = {w.name: w for w in WINDOWS}


def windows_of(territory: str) -> list[Window]:
    return [w for w in WINDOWS if w.territory == territory]


def find_window(lat: float, lon: float) -> Window | None:
    return next((w for w in WINDOWS if w.contains(lat, lon)), None)


@dataclass(frozen=True)
class Crop:
    """Les indices `[j0:j1, i0:i1]` d'une fenêtre dans une carte, et le premier nœud (latitude, longitude -180..180)."""

    j0: int
    j1: int
    i0: int
    i1: int
    lat0: float
    lon0: float
    dlat: float
    dlon: float

    @property
    def shape(self) -> tuple[int, int]:
        return self.j1 - self.j0, self.i1 - self.i0


def crop_of(grid: LatLonGrid, window: Window) -> Crop | None:
    """La découpe de la fenêtre dans la carte, ou None quand elle ne la recouvre pas entièrement."""
    nj, ni = grid.values.shape
    j0 = int(np.ceil((window.lat[0] - grid.lat0) / grid.dlat - 1e-9))
    j1 = int(np.floor((window.lat[1] - grid.lat0) / grid.dlat + 1e-9)) + 1
    east0 = (window.lon[0] - grid.lon0) % 360.0
    i0 = int(np.ceil(east0 / grid.dlon - 1e-9))
    i1 = i0 + int(np.floor((window.lon[1] - window.lon[0]) / grid.dlon + 1e-9)) + 1
    if j0 < 0 or i0 < 0 or j1 > nj or i1 > ni or j1 <= j0 or i1 <= i0:
        return None
    lon0 = ((grid.lon0 + i0 * grid.dlon + 180.0) % 360.0) - 180.0
    return Crop(j0, j1, i0, i1, grid.lat0 + j0 * grid.dlat, lon0, grid.dlat, grid.dlon)


@dataclass
class WindowSeries:
    """La série d'une fenêtre pour un run : `values[échéance, j, i, grandeur]` (float16)."""

    window: Window
    crop: Crop
    values: np.ndarray

    def node(self, lat: float, lon: float) -> tuple[int, int] | None:
        c = self.crop
        j = round((lat - c.lat0) / c.dlat)
        i = round((lon - c.lon0) / c.dlon)
        nj, ni = c.shape
        return (j, i) if 0 <= j < nj and 0 <= i < ni else None


@dataclass
class AromeOmRun:
    """Un run (réseau) : ses fenêtres, lues sur disque à la demande (`np.load` en `mmap_mode="r"`)."""

    run: datetime  # UTC
    directory: Path
    series: dict[str, WindowSeries]

    def node_latlon(self, lat: float, lon: float) -> tuple[float, float] | None:
        """La position du nœud servi pour un point (latitude, longitude de -180 à 180)."""
        window = find_window(lat, lon)
        if window is None or window.name not in self.series:
            return None
        found = self.series[window.name]
        node = found.node(lat, lon)
        if node is None:
            return None
        c = found.crop
        return round(c.lat0 + node[0] * c.dlat, 4), round(c.lon0 + node[1] * c.dlon, 4)

    def hours(self, lat: float, lon: float) -> dict[int, dict[str, float | None]] | None:
        """Les valeurs du nœud le plus proche, par échéance (heures depuis le run) : `{lead: {grandeur: valeur ou None}}`."""
        window = find_window(lat, lon)
        if window is None or window.name not in self.series:
            return None
        found = self.series[window.name]
        node = found.node(lat, lon)
        if node is None:
            return None
        raw = np.asarray(found.values[:, node[0], node[1], :], dtype=np.float32)
        out: dict[int, dict[str, float | None]] = {}
        for lead in range(raw.shape[0]):
            row: dict[str, float | None] = {}
            for k, name in enumerate(VARIABLES):
                v = float(raw[lead, k])
                if np.isnan(v):
                    row[name] = None
                else:
                    row[name] = v + PRESSURE_OFFSET_HPA if name == "pressure_hpa" else v
            out[lead] = row
        return out


class AromeOmStore:
    """Les runs sur disque : un dossier par run (`<epoch>/`, un `.npy` par fenêtre et `meta.json` écrit en dernier = run complet)."""

    def __init__(self, directory: str | Path, keep_runs: int = 2) -> None:
        self.directory = Path(directory)
        self.keep_runs = keep_runs
        self._memory: dict[int, AromeOmRun] = {}

    def work_dir(self, epoch: int) -> Path:
        return self.directory / f"{epoch}.part"

    def epochs(self) -> list[int]:
        if not self.directory.is_dir():
            return []
        found = []
        for entry in self.directory.iterdir():
            if entry.is_dir() and entry.name.isdigit() and (entry / "meta.json").is_file():
                found.append(int(entry.name))
        return sorted(found)

    def finish(self, epoch: int, crops: dict[str, Crop]) -> None:
        """Déclare le run complet : le dossier de travail devient `<epoch>/` avec son `meta.json`."""
        work = self.work_dir(epoch)
        meta = {
            name: {"j0": c.j0, "j1": c.j1, "i0": c.i0, "i1": c.i1, "lat0": c.lat0, "lon0": c.lon0, "dlat": c.dlat, "dlon": c.dlon}
            for name, c in crops.items()
        }
        (work / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        work.replace(self.directory / str(epoch))

    def load(self, epoch: int) -> AromeOmRun | None:
        folder = self.directory / str(epoch)
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            series: dict[str, WindowSeries] = {}
            for name, c in meta.items():
                if name in WINDOWS_BY_NAME:
                    series[name] = WindowSeries(WINDOWS_BY_NAME[name], Crop(**c), np.load(folder / f"{name}.npy", mmap_mode="r"))
        except (FileNotFoundError, ValueError, KeyError, OSError, TypeError):
            return None
        return AromeOmRun(datetime.fromtimestamp(epoch, tz=UTC), folder, series)

    def latest_runs(self, count: int = 2) -> list[AromeOmRun]:
        """Les `count` runs complets les plus récents, du plus récent au plus ancien."""
        epochs = self.epochs()
        runs = []
        for epoch in reversed(epochs):
            if epoch not in self._memory:
                loaded = self.load(epoch)
                if loaded is None:
                    continue
                self._memory[epoch] = loaded
            runs.append(self._memory[epoch])
            if len(runs) == count:
                break
        for stale in set(self._memory) - set(epochs):
            del self._memory[stale]
        return runs

    def purge(self) -> int:
        removed = 0
        for epoch in self.epochs()[: -self.keep_runs]:
            shutil.rmtree(self.directory / str(epoch), ignore_errors=True)
            removed += 1
        if self.directory.is_dir():
            limit = datetime.now(UTC).timestamp() - 6 * 3600
            for part in self.directory.glob("*.part"):
                if part.stat().st_mtime < limit:
                    shutil.rmtree(part, ignore_errors=True)
        return removed


def merged_series(runs: list[AromeOmRun], lat: float, lon: float) -> list[tuple[datetime, dict[str, float | None]]] | None:
    """La série horaire d'un point en recollant les runs : pour chaque heure, la valeur du run le plus récent qui l'a.

    Les heures avant le run le plus récent viennent du run précédent. La rafale et la pluie de l'échéance 0 manquent : l'heure garde
    alors celles du run précédent, ou rien. `None` si le point est hors fenêtre."""
    by_time: dict[datetime, dict[str, float | None]] = {}
    for run in reversed(runs):  # du plus ancien au plus récent : le récent écrase
        hours = run.hours(lat, lon)
        if hours is None:
            return None
        for lead, row in hours.items():
            when = run.run + timedelta(hours=lead)
            merged = dict(row)
            previous = by_time.get(when)
            if previous is not None:
                for name, value in row.items():
                    if value is None:
                        merged[name] = previous.get(name)
            by_time[when] = merged
    return sorted(by_time.items())
