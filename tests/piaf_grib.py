"""Fabrique de messages GRIB2 pour les tests : grille 3.0, empaquetage simple 5.0, comme le WCS PIAF.

La géométrie par défaut est celle du WCS PIAF relevée le 17/11/2026 : 1651 x 1051 nœuds au pas de 0,01°, premier nœud
(51,5° N ; 354° = -6° E), balayage du nord vers le sud.
"""

from __future__ import annotations

import struct

import numpy as np

PIAF_NI, PIAF_NJ = 1651, 1051
PIAF_LAT_NORTH, PIAF_LON_WEST = 51.5, 354.0


def _sm(value: int, bits: int) -> int:
    """Entier signé à la manière GRIB2 (bit de signe + valeur absolue)."""
    return (1 << (bits - 1)) | -value if value < 0 else value


def encode(
    values: np.ndarray,
    la1: float = PIAF_LAT_NORTH,
    lo1: float = PIAF_LON_WEST,
    step: float = 0.01,
    north_to_south: bool = True,
    decimal: int = 2,
    nbits: int = 16,
) -> bytes:
    """`values[j, i]`, la ligne 0 au premier nœud (au nord si `north_to_south`), en un message GRIB2."""
    nj, ni = values.shape
    packed = np.round(values.astype(np.float64) * 10**decimal).astype(np.int64)
    if packed.min() < 0 or packed.max() >= 2**nbits:
        raise ValueError("valeur hors de l'empaquetage")
    micro = lambda deg: int(round(deg * 1e6))  # noqa: E731
    la2 = la1 - step * (nj - 1) if north_to_south else la1 + step * (nj - 1)
    lo2 = lo1 + step * (ni - 1)
    s3 = struct.pack(
        ">IBBIBBH" + "B" + "B" * 15 + "IIII" + "I" + "I" + "B" + "I" + "I" + "IIB",
        72,
        3,
        0,
        ni * nj,
        0,
        0,
        0,
        6,
        *([0] * 15),
        ni,
        nj,
        0,
        0xFFFFFFFF,
        _sm(micro(la1), 32),
        micro(lo1) % (360 * 10**6),
        48,
        _sm(micro(la2), 32),
        micro(lo2) % (360 * 10**6),
        micro(step),
        micro(step),
        0 if north_to_south else 0x40,
    )
    assert len(s3) == 72, len(s3)
    s5 = struct.pack(">IBIHfHHBB", 21, 5, ni * nj, 0, 0.0, 0, _sm(decimal, 16), nbits, 0)
    s6 = struct.pack(">IBB", 6, 6, 255)
    flat = packed.reshape(-1)
    if nbits == 16:
        body = flat.astype(">u2").tobytes()
    else:
        bits = ((flat[:, None] >> np.arange(nbits - 1, -1, -1)) & 1).astype(np.uint8).reshape(-1)
        body = np.packbits(bits).tobytes()
    s7 = struct.pack(">IB", 5 + len(body), 7) + body
    s1 = struct.pack(">IB", 21, 1) + bytes(16)
    s4 = struct.pack(">IBHH", 9, 4, 0, 8)
    sections = s1 + s3 + s4 + s5 + s6 + s7
    total = 16 + len(sections) + 4
    return b"GRIB" + b"\x00\x00" + bytes([0, 2]) + struct.pack(">Q", total) + sections + b"7777"


_FILLED: dict[float, bytes] = {}


def piaf_filled(value: float) -> bytes:
    """Une échéance PIAF de toute la France où chaque nœud vaut `value` (mm en 5 min), gardée pour les tests suivants."""
    if value not in _FILLED:
        _FILLED[value] = encode(np.full((PIAF_NJ, PIAF_NI), value))
    return _FILLED[value]
