"""Les cartes de probabilités d'une prévision d'ensemble réduites à une grille grossière, et la lecture d'un point dedans.

Deux ensembles de Météo-France se lisent ici (un run de 35 membres chacun, la probabilité est la part des membres qui dépassent un seuil) :
- PE-AROME (`pearome/1.0`, 2,5 km, +51 h, 4 runs par jour à 03/09/15/21 h UTC) : Météo-France publie **déjà les probabilités**, une carte
  par seuil et par échéance (0,025°, 0,8 Mo en GRIB2) ; chaque carte est réduite à des blocs de 2 x 2 nœuds (0,05°, environ 5 km) par la moyenne
  (une probabilité se moyenne, contrairement à une rafale : la moyenne du bloc est l'estimation de la probabilité autour du point) ;
- PE-ARPEGE (`pearpege/1.0`, 0,1° sur l'Europe, jusqu'à +102 h, 4 runs par jour à 00/06/12/18 h UTC) : **un service par membre** (000 à 034) et
  des valeurs, pas des probabilités : la passerelle compte, pour chaque seuil, les membres dont le cumul de 24 h le dépasse.

Une valeur lue est « la probabilité autour du point » (bloc de 5 km ou maille de 10 km), pas celle du point exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from app.collectors.grib2 import LatLonGrid


def pool_mean(values: np.ndarray, pool: int) -> np.ndarray:
    """La moyenne par blocs de `pool` x `pool` nœuds (bords prolongés par leur dernière valeur)."""
    if pool == 1:
        return values.astype(np.float32)
    nj, ni = values.shape
    padded = np.pad(values, ((0, -nj % pool), (0, -ni % pool)), mode="edge")
    blocks = padded.reshape(padded.shape[0] // pool, pool, padded.shape[1] // pool, pool)
    return blocks.mean(axis=(1, 3), dtype=np.float32)


def crop_to_box(grid: LatLonGrid, south: float, west: float, north: float, east: float) -> LatLonGrid:
    """La partie de la grille qui couvre la boîte (nœuds du bord inclus), bornée à la grille ; `ValueError` si elle ne la touche pas.

    Les longitudes de GRIB2 vont de 0 à 360 : l'écart est mesuré à l'est du premier nœud, comme dans `LatLonGrid.index`.
    """
    nj, ni = grid.values.shape
    j0 = max(0, math.floor((south - grid.lat0) / grid.dlat))
    j1 = min(nj - 1, math.ceil((north - grid.lat0) / grid.dlat))
    i0 = max(0, math.floor(((west - grid.lon0) % 360.0) / grid.dlon))
    i1 = min(ni - 1, math.ceil(((east - grid.lon0) % 360.0) / grid.dlon))
    if j0 > j1 or i0 > i1:
        raise ValueError("la boîte ne touche pas la grille")
    return LatLonGrid(grid.values[j0 : j1 + 1, i0 : i1 + 1], grid.lat0 + j0 * grid.dlat, grid.lon0 + i0 * grid.dlon, grid.dlat, grid.dlon)


def to_percent(values: np.ndarray) -> np.ndarray:
    """Des pourcentages (parfois 100,57 : bruit de l'empaquetage) rangés dans un octet, bornés à 0..100, jamais enroulés."""
    return np.clip(np.rint(values), 0, 100).astype(np.uint8)


@dataclass(frozen=True)
class GridGeometry:
    """La grille d'origine (nœud `[0, 0]` = sud-ouest, `j` vers le nord, `i` vers l'est), son nombre de nœuds et la taille des blocs."""

    lat0: float
    lon0: float
    dlat: float
    dlon: float
    nj: int
    ni: int
    pool: int

    @classmethod
    def of(cls, grid: LatLonGrid, pool: int) -> GridGeometry:
        nj, ni = grid.values.shape
        return cls(grid.lat0, grid.lon0, grid.dlat, grid.dlon, nj, ni, pool)

    def cell(self, lat: float, lon: float) -> tuple[int, int] | None:
        """Le bloc (J, I) du nœud le plus proche d'un point, ou None hors de la carte."""
        j = round((lat - self.lat0) / self.dlat)
        east = (lon - self.lon0) % 360.0
        if east > 360.0 - self.dlon / 2:
            east -= 360.0
        i = round(east / self.dlon)
        if 0 <= j < self.nj and 0 <= i < self.ni:
            return j // self.pool, i // self.pool
        return None


@dataclass
class ProbabilityRun:
    """Un run : pour chaque grandeur, ses échéances (heures après le run) et sa carte de pourcentages `[échéance, J, I]` (octets)."""

    run: datetime  # UTC
    geometry: GridGeometry
    hours: dict[str, tuple[int, ...]]
    layers: dict[str, np.ndarray]

    def point(self, lat: float, lon: float) -> dict[str, dict[int, int]] | None:
        """`{grandeur: {heure: pourcentage}}` au bloc du point, ou None hors de la carte."""
        cell = self.geometry.cell(lat, lon)
        if cell is None:
            return None
        j, i = cell
        return {
            key: {hour: int(self.layers[key][k, j, i]) for k, hour in enumerate(hours)} for key, hours in self.hours.items()
        }
