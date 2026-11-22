"""Rendu en tuiles PNG Web Mercator de la pluie dans l'heure PIAF (Météo-France).

Les cartes PIAF (`LatLonGrid`, pas de 0,01°, cumul sur 5 min en mm) sont déjà gardées en mémoire
par `MeteoFranceNowcastCollector`. Ce module les projette à la demande en tuiles PNG 256 px
(zooms 3 à 10) avec exactement la même échelle d'intensité et la même palette que la mosaïque
radar (`app/radar_grid.py`) : aucune écriture disque et aucune requête Météo-France en plus.
"""

from __future__ import annotations

import numpy as np

from app.collectors.grib2 import LatLonGrid
from app.radar_grid import (
    MAX_ZOOM,
    MIN_ZOOM,
    MM_5MIN_TO_MM_H,
    THRESHOLDS_MM_H,
    TILE,
    TRANSPARENT_TILE,
    encode_png,
)

# Limite physique d'un cumul PIAF de 5 min valide (identique à `piaf_mm_5min` dans meteofrance.py).
_MAX_VALID_MM_5MIN = 500.0


def grid_bounds(grid: LatLonGrid) -> tuple[float, float, float, float]:
    """Boîte (ouest, sud, est, nord) en degrés WGS84 [-180, 180] couverte par la grille."""
    nj, ni = grid.values.shape
    west = ((grid.lon0 + 180.0) % 360.0) - 180.0
    east = west + (ni - 1) * grid.dlon
    south = grid.lat0
    north = grid.lat0 + (nj - 1) * grid.dlat
    return (round(west, 6), round(south, 6), round(east, 6), round(north, 6))


def classify_mm_5min(values: np.ndarray) -> np.ndarray:
    """Classes d'intensité (0..9) d'un tableau de cumuls 5 min en mm : 0 = sec ou invalide, 1..9 = palette radar."""
    thresholds = np.asarray(THRESHOLDS_MM_H, dtype=np.float32)
    valid = np.isfinite(values) & (values >= 0.0) & (values <= _MAX_VALID_MM_5MIN)
    mm_h = np.where(valid, values.astype(np.float32) * np.float32(MM_5MIN_TO_MM_H), np.float32(0.0))
    classes = np.searchsorted(thresholds, mm_h, side="right").astype(np.uint8)
    classes[~valid] = 0
    return classes


def piaf_tile_png(grid: LatLonGrid, z: int, x: int, y: int) -> bytes:
    """La tuile (z, x, y) Web Mercator d'une grille PIAF : PNG 256 px à palette + transparence, transparent sans pluie."""
    if not (MIN_ZOOM <= z <= MAX_ZOOM) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
        raise ValueError("tuile hors de la carte")

    n = 2**z
    edges = np.arange(TILE + 1, dtype=np.float64) / TILE
    lon_edges = (x + edges) / n * 360.0 - 180.0
    lat_edges = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * (y + edges) / n))))

    tile_west, tile_east = float(lon_edges[0]), float(lon_edges[-1])
    tile_north, tile_south = float(lat_edges[0]), float(lat_edges[-1])

    nj, ni = grid.values.shape
    grid_west = ((grid.lon0 + 180.0) % 360.0) - 180.0
    grid_east = grid_west + (ni - 1) * grid.dlon
    grid_south = grid.lat0
    grid_north = grid.lat0 + (nj - 1) * grid.dlat

    half_lat = grid.dlat * 0.5
    half_lon = grid.dlon * 0.5
    if (
        tile_north < grid_south - half_lat
        or tile_south > grid_north + half_lat
        or tile_east < grid_west - half_lon
        or tile_west > grid_east + half_lon
    ):
        return TRANSPARENT_TILE

    steps = (np.arange(TILE, dtype=np.float64) + 0.5) / TILE
    lon = (x + steps) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * (y + steps) / n))))

    px_dlat = (tile_north - tile_south) / TILE
    px_dlon = (tile_east - tile_west) / TILE

    # Zooms fins (pixel <= maille) : lecture directe au nœud le plus proche.
    if px_dlat <= grid.dlat and px_dlon <= grid.dlon:
        j = np.rint((lat - grid.lat0) / grid.dlat).astype(np.int64)
        i = np.rint((lon - grid_west) / grid.dlon).astype(np.int64)
        valid_j = (j >= 0) & (j < nj)
        valid_i = (i >= 0) & (i < ni)
        if not (valid_j.any() and valid_i.any()):
            return TRANSPARENT_TILE
        classes_sub = classify_mm_5min(grid.values[np.ix_(j[valid_j], i[valid_i])])
        if not classes_sub.any():
            return TRANSPARENT_TILE
        image = np.zeros((TILE, TILE), dtype=np.uint8)
        image[np.ix_(valid_j, valid_i)] = classes_sub
        return encode_png(image)

    # Zooms larges (un pixel couvre plusieurs nœuds) : classe maximale du pixel pour ne pas effacer une averse.
    j_lo = np.ceil((lat_edges[1:] - grid.lat0) / grid.dlat).astype(np.int64)
    j_hi = np.floor((lat_edges[:-1] - grid.lat0) / grid.dlat).astype(np.int64)
    j_c = np.rint((lat - grid.lat0) / grid.dlat).astype(np.int64)
    empty_j = j_lo > j_hi
    j_lo = np.where(empty_j, j_c, j_lo)
    j_hi = np.where(empty_j, j_c, j_hi)
    valid_r = (j_lo < nj) & (j_hi >= 0)
    j_lo = np.clip(j_lo, 0, nj - 1)
    j_hi = np.clip(j_hi, 0, nj - 1)

    i_lo = np.ceil((lon_edges[:-1] - grid_west) / grid.dlon).astype(np.int64)
    i_hi = np.floor((lon_edges[1:] - grid_west) / grid.dlon).astype(np.int64)
    i_c = np.rint((lon - grid_west) / grid.dlon).astype(np.int64)
    empty_i = i_lo > i_hi
    i_lo = np.where(empty_i, i_c, i_lo)
    i_hi = np.where(empty_i, i_c, i_hi)
    valid_c = (i_lo < ni) & (i_hi >= 0)
    i_lo = np.clip(i_lo, 0, ni - 1)
    i_hi = np.clip(i_hi, 0, ni - 1)

    if not (valid_r.any() and valid_c.any()):
        return TRANSPARENT_TILE

    r_idx = np.flatnonzero(valid_r)
    c_idx = np.flatnonzero(valid_c)
    j0 = int(j_lo[r_idx].min())
    j1 = int(j_hi[r_idx].max()) + 1
    i0 = int(i_lo[c_idx].min())
    i1 = int(i_hi[c_idx].max()) + 1

    sub_cls = classify_mm_5min(grid.values[j0:j1, i0:i1])
    if not sub_cls.any():
        return TRANSPARENT_TILE

    row_reduced = np.empty((len(r_idx), i1 - i0), dtype=np.uint8)
    for out_r, r in enumerate(r_idx):
        row_reduced[out_r] = sub_cls[j_lo[r] - j0 : j_hi[r] - j0 + 1].max(axis=0)

    tile_sub = np.empty((len(r_idx), len(c_idx)), dtype=np.uint8)
    for out_c, c in enumerate(c_idx):
        tile_sub[:, out_c] = row_reduced[:, i_lo[c] - i0 : i_hi[c] - i0 + 1].max(axis=1)

    if not tile_sub.any():
        return TRANSPARENT_TILE

    image = np.zeros((TILE, TILE), dtype=np.uint8)
    image[np.ix_(r_idx, c_idx)] = tile_sub
    return encode_png(image)
