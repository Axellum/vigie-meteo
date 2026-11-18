"""Fabrique de fichiers GRIB2 à plusieurs champs pour les tests : grille 3.0, empaquetage CCSDS 5.42 (ou simple 5.0), produits 4.0 et 4.8.

Les octets suivent les fichiers du PaquetAROME-OM relevés le 18/11/2026 (grille 0,025° balayée du nord vers le sud, longitudes de 0 à 360,
CCSDS sur 16 bits avec les drapeaux 14, bloc de 32, RSI de 128 ; le cumul de pluie en produit 4.8 avec le processus statistique 1)."""

from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta

import imagecodecs
import numpy as np

from tests.piaf_grib import _sm


def field_message(
    values: np.ndarray,
    key: tuple[int, int, int, int, int],
    reference: datetime,
    lead: int,
    la1: float,
    lo1: float,
    step: float = 0.025,
    stat: int | None = None,
    span: int = 1,
    packing: int = 42,
    decimal: int = 2,
    reference_value: float = 0.0,
) -> bytes:
    """`values[j, i]` (ligne 0 au nord) en un message GRIB2 ; `reference_value` est la référence R du GRIB2 (en unités mises à l'échelle) ; `stat` fait un produit 4.8 (cumul 1, maximum 2) sur `span` heures finissant à `lead`."""
    nj, ni = values.shape
    discipline, category, number, level_type, level_value = key
    packed = np.round(values.astype(np.float64) * 10**decimal - reference_value).astype(np.int64)
    assert packed.min() >= 0 and packed.max() < 2**16
    micro = lambda deg: int(round(deg * 1e6))  # noqa: E731
    s3 = struct.pack(
        ">IBBIBBH" + "B" + "B" * 15 + "IIII" + "I" + "I" + "B" + "I" + "I" + "IIB",
        72, 3, 0, ni * nj, 0, 0, 0, 6, *([0] * 15), ni, nj, 0, 0xFFFFFFFF,
        _sm(micro(la1), 32), micro(lo1) % (360 * 10**6), 48,
        _sm(micro(la1 - step * (nj - 1)), 32), micro(lo1 + step * (ni - 1)) % (360 * 10**6),
        micro(step), micro(step), 0,
    )  # fmt: skip
    assert len(s3) == 72
    flat = packed.reshape(-1).astype(">u2")
    if packing == 42:
        body = imagecodecs.aec_encode(flat, bitspersample=16, flags=14, blocksize=32, rsi=128)
        s5 = struct.pack(">IBIHfHHBBBBH", 25, 5, ni * nj, 42, reference_value, 0, _sm(decimal, 16), 16, 0, 14, 32, 128)
    else:
        body = flat.tobytes()
        s5 = struct.pack(">IBIHfHHBB", 21, 5, ni * nj, 0, reference_value, 0, _sm(decimal, 16), 16, 0)
    s6 = struct.pack(">IBB", 6, 6, 255)
    s7 = struct.pack(">IB", 5 + len(body), 7) + bytes(body)
    s1 = struct.pack(">IBHHBBB", 21, 1, 85, 0, 28, 0, 1) + struct.pack(">HBBBBB", reference.year, reference.month, reference.day, reference.hour, 0, 0) + bytes([0, 1])
    assert len(s1) == 21
    head = struct.pack(">IBHH", 34 if stat is None else 58, 4, 0, 0 if stat is None else 8) + bytes([category, number, 2, 0, 0]) + struct.pack(">HBBI", 0, 0, 1, lead if stat is None else 0)
    head += struct.pack(">BBIBBI", level_type, 0, level_value, 255, 0, 0)
    if stat is None:
        s4 = head
    else:
        end = reference.astimezone(UTC) + timedelta(hours=lead)
        end_year = struct.pack(">HBBBBB", end.year, end.month, end.day, end.hour, 0, 0)
        s4 = head + end_year + bytes([1]) + struct.pack(">I", 0) + bytes([stat, 2, 1]) + struct.pack(">I", span) + bytes([255]) + struct.pack(">I", 0)
    assert len(s4) == (34 if stat is None else 58), len(s4)
    sections = s1 + s3 + s4 + s5 + s6 + s7
    return b"GRIB" + b"\x00\x00" + bytes([discipline, 2]) + struct.pack(">Q", 16 + len(sections) + 4) + sections + b"7777"
