"""Décodeur GRIB2 de fichiers à plusieurs champs : grille 3.0, empaquetage simple (5.0) ou CCSDS (5.42).

C'est ce que rendent les « paquets » de Météo-France (PaquetAROME-OM, relevé le 18/11/2026) : un fichier de 4 à 12 Mo par échéance qui
range une quinzaine de champs, chacun dans son message, empaquetés en CCSDS (modèle 5.42, comme eccodes l'écrit avec `ccsds`). Le
décodage du CCSDS passe par `imagecodecs` (libaec) ; la lecture des sections et du modèle 3.0 est celle de `grib2.py`.

Un champ est identifié par `(discipline, catégorie, numéro, type de niveau, valeur du niveau)` : l'ordre des messages d'un fichier
n'est pas une garantie. Pour un champ calculé sur une période (maximum, cumul), `range_hours` et `stat` le disent.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from app.collectors.grib2 import LatLonGrid, _signed, _unpack

FieldKey = tuple[int, int, int, int, int]  # discipline, catégorie, numéro, type de niveau, valeur du niveau


@dataclass(frozen=True)
class Field:
    key: FieldKey
    reference_time: datetime  # le réseau (run), UTC
    lead_hours: int  # l'échéance de la valeur (fin de la période pour un champ statistique)
    stat: int | None  # processus statistique (table 4.10 : 1 cumul, 2 maximum) ; None pour une valeur instantanée
    range_hours: int  # durée de la période d'un champ statistique, 0 sinon
    grid: LatLonGrid


def _sections(data: bytes, pos: int, total: int) -> dict[int, bytes]:
    sections: dict[int, bytes] = {}
    p = pos + 16
    while p < pos + total - 4:
        length = struct.unpack(">I", data[p : p + 4])[0]
        if length < 5 or p + length > pos + total - 4:
            raise ValueError("section GRIB2 invalide")
        sections[data[p + 4]] = data[p : p + length]
        p += length
    return sections


def _values(s3: bytes, s5: bytes, s6: bytes | None, s7: bytes) -> np.ndarray:
    npoints = struct.unpack(">I", s3[6:10])[0]
    count = struct.unpack(">I", s5[5:9])[0]
    if count != npoints or (s6 is not None and s6[5] != 255):
        raise ValueError("masque GRIB2 (section 6) : non pris en charge")
    template = struct.unpack(">H", s5[9:11])[0]
    reference = struct.unpack(">f", s5[11:15])[0]
    e = _signed(struct.unpack(">H", s5[15:17])[0], 16)
    d = _signed(struct.unpack(">H", s5[17:19])[0], 16)
    nbits = s5[19]
    if nbits == 0:
        return np.full(count, reference / 10.0**d, dtype=np.float32)
    if template == 0:
        packed = _unpack(s7[5:], nbits, count)
    elif template == 42:
        import imagecodecs  # noqa: PLC0415 - dépendance lourde, chargée à la première lecture

        flags, block, rsi = s5[21], s5[22], struct.unpack(">H", s5[23:25])[0]
        raw = imagecodecs.aec_decode(bytes(s7[5:]), bitspersample=nbits, flags=flags, blocksize=block, rsi=rsi)
        width = (nbits + 7) // 8
        if width == 3:
            raise ValueError("CCSDS sur 3 octets : non pris en charge")
        packed = np.frombuffer(raw, dtype={1: ">u1", 2: ">u2", 4: ">u4"}[width], count=count).astype(np.uint32)
    else:
        raise ValueError(f"empaquetage GRIB2 {template} non pris en charge")
    return ((reference + packed.astype(np.float64) * 2.0**e) / 10.0**d).astype(np.float32)


def _grid(s3: bytes, flat: np.ndarray) -> LatLonGrid:
    if struct.unpack(">H", s3[12:14])[0] != 0:
        raise ValueError("grille GRIB2 autre que latitude/longitude régulière (modèle 3.0)")
    ni, nj = struct.unpack(">II", s3[30:38])
    basic, subdiv = struct.unpack(">II", s3[38:46])
    unit = 1e-6 if basic in (0, 0xFFFFFFFF) or subdiv in (0, 0xFFFFFFFF) else basic / subdiv
    la1 = _signed(struct.unpack(">I", s3[46:50])[0], 32) * unit
    lo1 = _signed(struct.unpack(">I", s3[50:54])[0], 32) * unit
    di = struct.unpack(">I", s3[63:67])[0] * unit
    dj = struct.unpack(">I", s3[67:71])[0] * unit
    scan = s3[71]
    if ni * nj != flat.size or scan & 0x20:
        raise ValueError("grille GRIB2 incohérente ou balayée par colonnes")
    grid = flat.reshape(nj, ni)
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


_UNIT_HOURS = {0: 1 / 60, 1: 1, 2: 24}


def decode_fields(data: bytes, wanted: set[FieldKey] | None = None):
    """Les champs d'un fichier GRIB2, un à un (générateur : un seul champ décodé existe à la fois). `wanted` : ne décoder que ceux-là."""
    pos = 0
    while pos < len(data):
        if data[pos : pos + 4] != b"GRIB" or data[pos + 7] != 2:
            raise ValueError("pas un message GRIB édition 2")
        total = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
        if pos + total > len(data) or data[pos + total - 4 : pos + total] != b"7777":
            raise ValueError("message GRIB2 tronqué")
        sections = _sections(data, pos, total)
        discipline = data[pos + 6]
        s4 = sections[4]
        template4 = struct.unpack(">H", s4[7:9])[0]
        key: FieldKey = (discipline, s4[9], s4[10], s4[22], struct.unpack(">I", s4[24:28])[0])
        if wanted is None or key in wanted:
            s1 = sections[1]
            year, month, day, hour, minute, second = struct.unpack(">HBBBBB", s1[12:19])
            reference = datetime(year, month, day, hour, minute, second, tzinfo=UTC)
            lead = struct.unpack(">I", s4[18:22])[0] * _UNIT_HOURS.get(s4[17], 0)
            stat, span = None, 0
            if template4 == 8:  # période : fin de l'intervalle, processus statistique, durée
                end = datetime(*struct.unpack(">HBBBBB", s4[34:41]), tzinfo=UTC)
                lead = (end - reference).total_seconds() / 3600
                stat = s4[46]
                span = int(struct.unpack(">I", s4[49:53])[0] * _UNIT_HOURS.get(s4[48], 0))
            flat = _values(sections[3], sections[5], sections.get(6), sections[7])
            yield Field(key, reference, int(round(lead)), stat, span, _grid(sections[3], flat))
        pos += total
