"""Mosaïque radar Météo-France (lame d'eau, métropole) : lecture du fichier HDF5, projection, tuiles PNG.

Fichier lu le 18/11/2026 (`DPRadar/v1/mosaiques/METROPOLE/observations/LAME_D_EAU/produit?maille=500`, HDF5 « H5rad 2.3 »,
2 Mo) : une image `dataset1/data1/data` de 3 472 x 3 472 entiers 16 bits, `ACRR` (cumul sur 5 min, `gain` 0,01 mm, `0` = sec,
`65535` = hors couverture des radars, 51 % de la grille), en projection stéréographique polaire (`+proj=stere +lat_0=90
+lon_0=0 +lat_ts=45 +ellps=WGS84`), pixels de 500 m, coin haut-gauche à (0 ; 1 736 000) m. La formule de projection
reproduit les quatre coins du fichier à 4 mm près (`tests/test_radar_grid.py`) ; le calage a été comparé à la carte PIAF
(corrélation maximale sans décalage, nettement plus basse à 4 pixels).

La carte est gardée sous forme de classes d'intensité (octet par pixel), pas en mm : c'est tout ce que la tuile montre.
Pas de h5py hors de ce module, pas de pyproj : la projection est écrite ici (numpy).
"""

from __future__ import annotations

import io
import struct
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

SIZE = 3472
PIXEL_M = 500.0
TOP_M = SIZE * PIXEL_M  # ordonnée du bord haut de l'image (le bord gauche est à x = 0)
EXPECTED_PROJ = ("+proj=stere", "+lat_0=90", "+lon_0=0", "+lat_ts=45", "+ellps=WGS84", "+x_0=619652.07", "+y_0=5262818.34")
UNDETECT = 65534
NODATA = 65535
# 5 min d'accumulation en mm -> mm/h
MM_5MIN_TO_MM_H = 12.0

# Seuils des classes d'intensité (mm/h) : en dessous du premier, ou hors couverture, rien n'est dessiné.
THRESHOLDS_MM_H = (0.1, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
# Une couleur (R, V, B, A) par classe 1..9 ; la classe 0 est transparente.
PALETTE = (
    (0, 0, 0, 0),
    (166, 206, 227, 170),
    (96, 165, 250, 200),
    (37, 99, 235, 215),
    (34, 197, 94, 225),
    (250, 204, 21, 235),
    (249, 115, 22, 240),
    (220, 38, 38, 245),
    (162, 28, 175, 250),
    (88, 28, 135, 255),
)

# Boîte (ouest, sud, est, nord) en degrés qui contient la grille : coins du fichier du 18/11/2026.
BOUNDS = (-9.97, 37.45, 17.57, 53.67)

TILE = 256
MIN_ZOOM = 3
MAX_ZOOM = 10

_A = 6378137.0
_F = 1 / 298.257223563
_E = (2 * _F - _F * _F) ** 0.5
_X0 = 619652.07
_Y0 = 5262818.34


def _t(phi: np.ndarray | float) -> np.ndarray | float:
    s = np.sin(phi)
    return np.tan(np.pi / 4 - phi / 2) / (((1 - _E * s) / (1 + _E * s)) ** (_E / 2))


_PHI_C = np.radians(45.0)
_M_C = np.cos(_PHI_C) / np.sqrt(1 - (_E * np.sin(_PHI_C)) ** 2)
_T_C = _t(_PHI_C)


def project(lat: np.ndarray | float, lon: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
    """(x, y) en mètres, dans le repère du fichier, d'un point latitude/longitude WGS84 (stéréographique polaire nord)."""
    rho = _A * _M_C * _t(np.radians(lat)) / _T_C
    lam = np.radians(lon)
    return _X0 + rho * np.sin(lam), _Y0 - rho * np.cos(lam)


def classify(values: np.ndarray) -> np.ndarray:
    """Classes d'intensité (octet) d'une image ACRR brute : 0 = pas de pluie ou hors couverture, 1..9 = intensité croissante.

    Traitée par bandes de lignes : d'un bloc, `searchsorted` rendrait 8 octets par pixel (96 Mo) et la conversion en mm/h
    48 Mo de plus, de quoi dépasser la mémoire de la passerelle."""
    thresholds = np.asarray(THRESHOLDS_MM_H, dtype=np.float32)
    classes = np.empty(values.shape, dtype=np.uint8)
    for start in range(0, values.shape[0], 256):
        band = values[start : start + 256]
        mm_h = band.astype(np.float32) * (0.01 * MM_5MIN_TO_MM_H)
        out = np.searchsorted(thresholds, mm_h, side="right").astype(np.uint8)
        out[band >= UNDETECT] = 0
        classes[start : start + 256] = out
    return classes


@dataclass
class RadarFrame:
    """Une image : la fin de ses 5 minutes de cumul et ses classes d'intensité."""

    end: datetime
    classes: np.ndarray  # uint8, (SIZE, SIZE)
    _pools: dict[int, np.ndarray] = field(default_factory=dict, repr=False)

    @property
    def epoch(self) -> int:
        return int(self.end.timestamp())

    def pooled(self, factor: int) -> np.ndarray:
        """L'image réduite d'un facteur 1, 2, 4 ou 8 : la classe maximale de chaque bloc (une averse reste visible)."""
        if factor == 1:
            return self.classes
        if factor not in (2, 4, 8):
            raise ValueError("facteur de réduction inconnu")
        if factor not in self._pools:
            n = SIZE // factor
            self._pools[factor] = self.classes.reshape(n, factor, n, factor).max(axis=(1, 3))
        return self._pools[factor]


def _text(value: object) -> str:
    """Un attribut HDF5 texte : octets dans le vrai fichier, `str` selon la version de h5py."""
    return value.decode() if isinstance(value, bytes) else str(value)


def decode_hdf5(content: bytes) -> RadarFrame:
    """L'image d'un fichier mosaïque `LAME_D_EAU` à 500 m. `ValueError` si le fichier n'est pas celui qu'on sait lire."""
    import h5py  # import tardif : la bibliothèque est lourde et seul ce chemin s'en sert

    try:
        f = h5py.File(io.BytesIO(content), "r")
    except OSError as exc:
        raise ValueError(f"fichier HDF5 illisible : {exc}") from exc
    with f:
        try:
            where = f["where"].attrs
            proj = _text(where["projdef"])
            if any(token not in proj for token in EXPECTED_PROJ):
                raise ValueError(f"projection inattendue : {proj[:120]}")
            if int(where["xsize"]) != SIZE or int(where["ysize"]) != SIZE or float(where["xscale"]) != PIXEL_M:
                raise ValueError("grille inattendue (taille ou pas)")
            what = f["dataset1/data1/what"].attrs
            quantity = _text(what["quantity"])
            if quantity != "ACRR" or float(what["gain"]) != 0.01 or float(what["offset"]) != 0.0:
                raise ValueError("grandeur inattendue (ACRR, 0,01 mm attendus)")
            if int(what["nodata"]) != NODATA or int(what["undetect"]) != UNDETECT:
                raise ValueError("codes nodata/undetect inattendus")
            when = f["dataset1/what"].attrs
            end = datetime.strptime(_text(when["enddate"]) + _text(when["endtime"]), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            data = f["dataset1/data1/data"][:]
        except KeyError as exc:
            raise ValueError(f"champ absent du fichier : {exc}") from exc
    if data.shape != (SIZE, SIZE) or data.dtype != np.uint16:
        raise ValueError("image inattendue")
    return RadarFrame(end=end, classes=classify(data))


# ------------------------------------------------------------------------------------------------ tuiles


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def encode_png(indexed: np.ndarray) -> bytes:
    """Un PNG à palette (8 bits) de l'image de classes ; la transparence vient de la table `tRNS`."""
    height, width = indexed.shape
    rows = np.zeros((height, width + 1), dtype=np.uint8)  # un octet de filtre (0) devant chaque ligne
    rows[:, 1:] = indexed
    palette = b"".join(bytes(c[:3]) for c in PALETTE)
    alpha = bytes(c[3] for c in PALETTE)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0))
        + _chunk(b"PLTE", palette)
        + _chunk(b"tRNS", alpha)
        + _chunk(b"IDAT", zlib.compress(rows.tobytes(), 6))
        + _chunk(b"IEND", b"")
    )


TRANSPARENT_TILE = encode_png(np.zeros((TILE, TILE), dtype=np.uint8))


def pool_factor(zoom: int) -> int:
    """Le facteur de réduction de l'image à ce zoom : un pixel d'écran vaut 1 à 8 pixels de 500 m (calculé à 46,5° N)."""
    metres_per_pixel = 156543.03 * np.cos(np.radians(46.5)) / (2**zoom)
    for factor, limit in ((1, 1000.0), (2, 2000.0), (4, 4000.0)):
        if metres_per_pixel < limit:
            return factor
    return 8


def tile_png(frame: RadarFrame, z: int, x: int, y: int) -> bytes:
    """La tuile (z, x, y) de la carte Web Mercator (schéma « slippy map ») : PNG de 256 px, transparent où il ne pleut pas."""
    if not (0 <= z <= MAX_ZOOM) or not (0 <= x < 2**z) or not (0 <= y < 2**z):
        raise ValueError("tuile hors de la carte")
    n = 2**z
    steps = (np.arange(TILE) + 0.5) / TILE
    lon = (x + steps) / n * 360.0 - 180.0
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1.0 - 2.0 * (y + steps) / n))))
    lon_grid, lat_grid = np.meshgrid(lon, lat)
    px, py = project(lat_grid, lon_grid)
    factor = pool_factor(z)
    cell = PIXEL_M * factor
    col = np.floor(px / cell).astype(np.int64)
    row = np.floor((TOP_M - py) / cell).astype(np.int64)
    limit = SIZE // factor
    inside = (col >= 0) & (col < limit) & (row >= 0) & (row < limit)
    if not inside.any():
        return TRANSPARENT_TILE
    image = np.zeros((TILE, TILE), dtype=np.uint8)
    image[inside] = frame.pooled(factor)[row[inside], col[inside]]
    if not image.any():
        return TRANSPARENT_TILE
    return encode_png(image)
