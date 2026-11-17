"""Décodeur GRIB2 minimal : un message, grille latitude/longitude régulière, empaquetage simple.

C'est exactement ce que renvoie le WCS PIAF de Météo-France en `application/wmo-grib` (relevé le 17/11/2026 :
un message, modèle de grille 3.0, modèle de produit 4.8, empaquetage 5.0 sur 16 bits, pas de masque) : 3,5 Mo
par échéance pour toute la France, contre 13,9 Mo en GeoTIFF. Pas d'eccodes (bibliothèque C) : numpy suffit.
Tout autre cas (autre grille, autre empaquetage, masque, plusieurs messages) lève `ValueError`.

Références : WMO Manual on Codes, FM 92 GRIB edition 2, sections 0 à 8 ; modèles 3.0 et 5.0.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LatLonGrid:
    """Valeurs sur une grille régulière : `values[j, i]` au nœud (`lat0 + j*dlat`, `lon0 + i*dlon`)."""

    values: np.ndarray  # float32, forme (nj, ni)
    lat0: float
    lon0: float
    dlat: float  # positif : la ligne j+1 est au nord de la ligne j
    dlon: float

    def index(self, lat: float, lon: float) -> tuple[int, int] | None:
        """Le nœud le plus proche d'un point, ou None hors de la grille (au-delà d'un demi-pas du bord)."""
        j = round((lat - self.lat0) / self.dlat)
        # GRIB2 compte les longitudes de 0 à 360 (PIAF : -6° est écrit 354°) : on mesure l'écart à l'est de lon0.
        east = (lon - self.lon0) % 360.0
        if east > 360.0 - self.dlon / 2:
            east -= 360.0  # un point à moins d'un demi-pas à l'ouest du premier nœud
        i = round(east / self.dlon)
        nj, ni = self.values.shape
        if 0 <= j < nj and 0 <= i < ni:
            return j, i
        return None


def _signed(raw: int, bits: int) -> int:
    """Entier signé GRIB2 : bit de poids fort = signe, le reste = valeur absolue (pas de complément à deux)."""
    sign = 1 << (bits - 1)
    return -(raw & (sign - 1)) if raw & sign else raw


def _unpack(data: bytes, nbits: int, count: int) -> np.ndarray:
    """Les `count` entiers non signés de `nbits` bits, collés à la suite, en grand-boutiste."""
    if nbits == 0:
        return np.zeros(count, dtype=np.uint32)
    if nbits in (8, 16, 32):
        dtype = {8: ">u1", 16: ">u2", 32: ">u4"}[nbits]
        return np.frombuffer(data, dtype=dtype, count=count).astype(np.uint32)
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8))[: count * nbits].reshape(count, nbits)
    weights = (1 << np.arange(nbits - 1, -1, -1, dtype=np.uint64)).astype(np.uint64)
    return (bits.astype(np.uint64) @ weights).astype(np.uint32)


def decode_latlon_simple(data: bytes) -> LatLonGrid:
    """Décode un message GRIB2 (grille 3.0, empaquetage 5.0, sans masque) en grille latitude/longitude."""
    if data[:4] != b"GRIB" or len(data) < 16 or data[7] != 2:
        raise ValueError("pas un message GRIB édition 2")
    total = struct.unpack(">Q", data[8:16])[0]
    if total > len(data):
        raise ValueError("message GRIB2 tronqué")
    if data[total - 4 : total] != b"7777":
        raise ValueError("fin de message GRIB2 absente")

    sections: dict[int, bytes] = {}
    pos = 16
    while pos < total - 4:
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        number = data[pos + 4]
        if length < 5 or pos + length > total - 4:
            raise ValueError("section GRIB2 invalide")
        if number in sections:
            raise ValueError("plusieurs champs dans le message GRIB2 : non pris en charge")
        sections[number] = data[pos : pos + length]
        pos += length
    for needed in (3, 5, 7):
        if needed not in sections:
            raise ValueError(f"section {needed} absente du message GRIB2")

    s3, s5, s7 = sections[3], sections[5], sections[7]
    # Section 3, modèle 3.0 (octets numérotés à partir de 1 dans le manuel de l'OMM : décalage de 1 ici).
    if struct.unpack(">H", s3[12:14])[0] != 0:
        raise ValueError("grille GRIB2 autre que latitude/longitude régulière (modèle 3.0)")
    npoints = struct.unpack(">I", s3[6:10])[0]
    ni, nj = struct.unpack(">II", s3[30:38])
    basic, subdiv = struct.unpack(">II", s3[38:46])
    unit = 1e-6 if basic in (0, 0xFFFFFFFF) or subdiv in (0, 0xFFFFFFFF) else basic / subdiv
    la1 = _signed(struct.unpack(">I", s3[46:50])[0], 32) * unit
    lo1 = _signed(struct.unpack(">I", s3[50:54])[0], 32) * unit
    di = struct.unpack(">I", s3[63:67])[0] * unit
    dj = struct.unpack(">I", s3[67:71])[0] * unit
    scan = s3[71]
    if ni * nj != npoints:
        raise ValueError("grille GRIB2 incohérente (Ni x Nj ≠ nombre de points)")
    if scan & 0x20:
        raise ValueError("balayage GRIB2 par colonnes : non pris en charge")

    # Section 5, modèle 5.0 : Y = (R + X * 2^E) / 10^D.
    if struct.unpack(">H", s5[9:11])[0] != 0:
        raise ValueError("empaquetage GRIB2 autre que simple (modèle 5.0)")
    count = struct.unpack(">I", s5[5:9])[0]
    if count != npoints:
        raise ValueError("nombre de valeurs GRIB2 ≠ nombre de points (masque non pris en charge)")
    reference = struct.unpack(">f", s5[11:15])[0]
    e = _signed(struct.unpack(">H", s5[15:17])[0], 16)
    d = _signed(struct.unpack(">H", s5[17:19])[0], 16)
    nbits = s5[19]
    if 6 in sections and sections[6][5] != 255:
        raise ValueError("masque GRIB2 (section 6) : non pris en charge")

    packed = _unpack(s7[5:], nbits, count)
    values = ((reference + packed.astype(np.float64) * 2.0**e) / 10.0**d).astype(np.float32)
    grid = values.reshape(nj, ni)

    # Ramener à : j croissant vers le nord, i croissant vers l'est.
    dlat = dj if scan & 0x40 else -dj
    dlon = -di if scan & 0x80 else di
    lat0, lon0 = la1, lo1
    if dlat < 0:
        grid = grid[::-1]
        lat0, dlat = la1 + dlat * (nj - 1), -dlat
    if dlon < 0:
        grid = grid[:, ::-1]
        lon0, dlon = lo1 + dlon * (ni - 1), -dlon
    return LatLonGrid(values=np.ascontiguousarray(grid), lat0=lat0, lon0=lon0, dlat=dlat, dlon=dlon)
