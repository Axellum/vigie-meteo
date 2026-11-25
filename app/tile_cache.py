"""Cache mémoire des tuiles PNG (radar et PIAF), borné en octets, éviction du moins récemment servi.

Chaque processus de la passerelle a le sien (deux processus : la mémoire utilisée au plus est le double de la borne).
La clé contient tout ce qui détermine l'image ; un nouveau run PIAF ou une nouvelle image radar donne donc d'autres clés,
et une ancienne tuile ne peut pas être servie à leur place (elle vieillit jusqu'à être évincée).
"""

from __future__ import annotations

import threading
import zlib
from collections import OrderedDict
from collections.abc import Hashable

from app.radar_grid import PALETTE, THRESHOLDS_MM_H

# Octets comptés en plus du PNG pour chaque entrée (clé, nœud du dictionnaire, objet bytes) : la borne reste vraie en mémoire.
ENTRY_OVERHEAD_BYTES = 200

# Identifiant du style (palette et seuils d'intensité) : change si l'une ou l'autre change, la tuile aussi.
STYLE_ID = zlib.crc32(repr((PALETTE, THRESHOLDS_MM_H)).encode()) & 0xFFFFFFFF


def radar_key(area: str, epoch: int, source_stamp: int, z: int, x: int, y: int) -> tuple[Hashable, ...]:
    """Clé d'une tuile radar : zone, fin de cumul de l'image, date d'écriture de son fichier, z/x/y, style."""
    return ("radar", area, epoch, source_stamp, z, x, y, STYLE_ID)


def piaf_key(run_epoch: int, epoch: int, step_seconds: int, z: int, x: int, y: int) -> tuple[Hashable, ...]:
    """Clé d'une tuile PIAF : run, fin de cumul de l'échéance, pas, z/x/y, style."""
    return ("piaf", run_epoch, epoch, step_seconds, z, x, y, STYLE_ID)


class TileCache:
    """LRU de tuiles PNG bornée en octets (`max_bytes` = 0 : cache coupé)."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max(0, max_bytes)
        self._entries: OrderedDict[Hashable, bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def size_bytes(self) -> int:
        """Octets comptés (PNG et surcoût des entrées) ; ne dépasse jamais `max_bytes`."""
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: Hashable) -> bytes | None:
        with self._lock:
            png = self._entries.get(key)
            if png is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return png

    def put(self, key: Hashable, png: bytes) -> None:
        cost = len(png) + ENTRY_OVERHEAD_BYTES
        if cost > self.max_bytes:  # coupé, ou une seule tuile plus grosse que tout le cache
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old) + ENTRY_OVERHEAD_BYTES
            self._entries[key] = png
            self._bytes += cost
            while self._bytes > self.max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted) + ENTRY_OVERHEAD_BYTES
