"""Les cartes d'un run AROME-PI réduites à une grille de 0,04° et la lecture d'un point dedans.

AROME-PI (Météo-France, prévision immédiate, run toutes les heures, pas de 15 min jusqu'à +6 h) est lu **par carte de la
France, pas par point** (comme PIAF) : une requête WCS par produit et par échéance, 24 échéances x 4 produits = 96 requêtes
par run. Chaque carte (1651 x 1051 nœuds à 0,01°, 3,5 Mo en GRIB2) est réduite tout de suite à des blocs de 4 x 4 nœuds
(0,04°, environ 4,4 km x 2,9 km) : la grille d'un run tient alors dans 15 Mo au lieu de 600 Mo.

La réduction garde le pire du bloc, pas la moyenne : le maximum pour les rafales et la grêle, le minimum pour la visibilité.
Pour le type de précipitation (un code, qu'on ne moyenne pas) : la valeur du nœud central du bloc, sauf si un nœud du
bloc est verglaçant ou de la grêle, qui l'emportent (les plus graves). Une valeur lue est donc « le pire autour du point »,
à ne pas confondre avec la valeur exacte au point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from app.collectors.grib2 import LatLonGrid

POOL = 4
STEPS = 24  # 15 min à 6 h
STEP_SECONDS = 900
# Codes de type de précipitation qui l'emportent sur la valeur centrale d'un bloc, du moins grave au plus grave
# (Météo-France, « Comprendre les diagnostics de type de précipitation dans les modèles AROME » : 12 bruine verglaçante,
# 10 grêle, 3 précipitations verglaçantes).
PTYPE_OVERRIDES = (12, 10, 3)


def _blocks(values: np.ndarray) -> np.ndarray:
    """La grille en blocs `[J, a, I, b]` de POOL x POOL nœuds (bords prolongés par leur dernière valeur)."""
    nj, ni = values.shape
    padded = np.pad(values, ((0, -nj % POOL), (0, -ni % POOL)), mode="edge")
    return padded.reshape(padded.shape[0] // POOL, POOL, padded.shape[1] // POOL, POOL)


def pool_max(values: np.ndarray) -> np.ndarray:
    return _blocks(values).max(axis=(1, 3))


def pool_min(values: np.ndarray) -> np.ndarray:
    return _blocks(values).min(axis=(1, 3))


def pool_code(values: np.ndarray) -> np.ndarray:
    """Un code par bloc : le nœud central, sauf verglas ou grêle dans le bloc (les plus graves l'emportent)."""
    blocks = _blocks(values)
    out = blocks[:, POOL // 2, :, POOL // 2].copy()
    for code in PTYPE_OVERRIDES:
        out[(blocks == code).any(axis=(1, 3))] = code
    return out


@dataclass(frozen=True)
class Geometry:
    """La grille d'origine (nœud `[0, 0]` = sud-ouest, `j` vers le nord, `i` vers l'est) et son nombre de nœuds."""

    lat0: float
    lon0: float
    dlat: float
    dlon: float
    nj: int
    ni: int

    @classmethod
    def of(cls, grid: LatLonGrid) -> Geometry:
        nj, ni = grid.values.shape
        return cls(grid.lat0, grid.lon0, grid.dlat, grid.dlon, nj, ni)

    def cell(self, lat: float, lon: float) -> tuple[int, int] | None:
        """Le bloc (J, I) du nœud le plus proche d'un point, ou None hors de la carte."""
        # Même calcul que `LatLonGrid.index`, sans la carte entière.
        j = round((lat - self.lat0) / self.dlat)
        east = (lon - self.lon0) % 360.0
        if east > 360.0 - self.dlon / 2:
            east -= 360.0
        i = round(east / self.dlon)
        if 0 <= j < self.nj and 0 <= i < self.ni:
            return j // POOL, i // POOL
        return None


@dataclass
class AromePiRun:
    """Un run complet : `gust_kmh`, `hail`, `visibility_m`, `ptype` de forme `[échéance, J, I]` (échéance k = fin de `run + (k+1) x 15 min`)."""

    run: datetime  # UTC
    geometry: Geometry
    gust_kmh: np.ndarray  # uint8
    hail: np.ndarray  # float16, kg/m2
    visibility_m: np.ndarray  # uint16
    ptype: np.ndarray  # uint8

    def point(self, lat: float, lon: float) -> dict[str, list] | None:
        cell = self.geometry.cell(lat, lon)
        if cell is None:
            return None
        j, i = cell
        return {
            "gust_kmh": self.gust_kmh[:, j, i].astype(int).tolist(),
            "hail_kg_m2": [round(float(v), 2) for v in self.hail[:, j, i]],
            "visibility_m": self.visibility_m[:, j, i].astype(int).tolist(),
            "precip_type": self.ptype[:, j, i].astype(int).tolist(),
        }
