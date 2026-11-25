"""Limiteur commun du quota du portail Météo-France : une clé, 50 requêtes par minute, tous produits et tous processus confondus.

Chaque collecteur a son propre rythme (`METEO_*_REQUESTS_PER_MINUTE`), mais la somme de ces plafonds dépasse 50 et les deux
processus de la passerelle comptent chacun de leur côté : au démarrage, tout part en même temps et le portail répond 429.
Ici, une seule comptabilité, partagée par les processus dans un petit fichier SQLite à côté de la base du cache :

- une fenêtre glissante de `limit` requêtes sur `window` secondes (45 sur 60 : une marge sous les 50 du portail) ;
- les produits « en gros » (cartes de prévision d'ensemble, AROME-PI, AROME-OM : des centaines de requêtes par run) ne
  peuvent prendre que `limit - reserve` des places ; le reste est gardé pour la pluie PIAF, le radar, DPClim et la
  météo des forêts, qu'un visiteur attend ;
- un petit écart minimal entre deux requêtes (`min_gap`), pour que le premier tour ne parte pas en rafale ;
- après un 429, plus aucune requête ne part avant la fin du délai demandé (`Retry-After`, 30 s sans lui) : tous les
  processus attendent, personne ne reboucle sur un refus.

Choix du fichier SQLite plutôt qu'un verrou de fichier : le dépôt a déjà SQLite (WAL), il marche sous Windows comme sous Linux
(les tests s'y lancent), et `BEGIN IMMEDIATE` donne l'exclusion entre processus et la lecture-puis-écriture du compteur d'un seul
tenant. Un verrou de fichier demanderait en plus un fichier d'état à relire et à réécrire sous verrou, donc la même chose en plus
fragile. Le fichier survit à un redémarrage du conteneur : le quota déjà consommé reste compté.

Ce que le limiteur ne voit pas : d'autres programmes qui utilisent la même clé (hors de cette passerelle) ; c'est la raison de
la marge de 5 requêtes par minute et du délai après un 429.
"""

from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

HOST_SUFFIX = "meteofrance.fr"
# Des produits qui coûtent des centaines de requêtes par run : ils ne prennent pas les places réservées à la pluie PIAF et au radar.
BULK_PATH_MARKERS = ("/aromepi/", "/pearome/", "/pearpege/", "/DPPaquetAROME-OM/")
MAX_COOLDOWN_S = 120.0


def is_meteofrance(host: str) -> bool:
    return host == HOST_SUFFIX or host.endswith("." + HOST_SUFFIX)


def is_bulk(path: str) -> bool:
    return any(marker in path for marker in BULK_PATH_MARKERS)


class SharedQuota:
    """Fenêtre glissante de requêtes partagée entre processus (fichier SQLite) ; temps et sommeil injectables pour les tests."""

    def __init__(
        self,
        path: str,
        limit: int = 45,
        reserve: int = 15,
        window: float = 60.0,
        min_gap: float = 0.25,
        default_cooldown: float = 30.0,
        jitter: float = 0.05,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.limit = limit
        self.bulk_limit = max(1, limit - reserve)
        self.window = window
        self.min_gap = min_gap
        self.default_cooldown = default_cooldown
        self._jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self.last_grant_ts: float | None = None
        self._conn = sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=10000;")
        self._conn.execute("CREATE TABLE IF NOT EXISTS grants (ts REAL NOT NULL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value REAL NOT NULL)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def try_acquire(self, bulk: bool = False) -> float:
        """Prend une place si elle est libre (rend 0.0), sinon rend le nombre de secondes à attendre avant de réessayer.

        Tout se passe sous `BEGIN IMMEDIATE` : deux processus ne peuvent pas compter la même place libre. L'heure est lue sous le verrou, pour que
        les dates écrites suivent l'ordre des écritures.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                self._conn.execute("DELETE FROM grants WHERE ts <= ?", (now - self.window,))
                stamps = [row[0] for row in self._conn.execute("SELECT ts FROM grants ORDER BY ts")]
                blocked = self._conn.execute("SELECT value FROM state WHERE key = 'blocked_until'").fetchone()
                wait = max(0.0, (blocked[0] if blocked else 0.0) - now)
                cap = self.bulk_limit if bulk else self.limit
                if len(stamps) >= cap:  # les (n - cap + 1) plus vieilles doivent sortir de la fenêtre
                    wait = max(wait, stamps[len(stamps) - cap] + self.window - now)
                if stamps:
                    wait = max(wait, stamps[-1] + self.min_gap - now)
                if wait <= 0.0:
                    self._conn.execute("INSERT INTO grants (ts) VALUES (?)", (now,))
                    self.last_grant_ts = now
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return wait

    def block(self, seconds: float) -> float:
        """Plus aucune requête pendant `seconds` (le portail a répondu 429) ; rend la durée retenue."""
        seconds = min(max(seconds, 1.0), MAX_COOLDOWN_S)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                until = self._clock() + seconds
                self._conn.execute(
                    "INSERT INTO state (key, value) VALUES ('blocked_until', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = max(value, excluded.value)",
                    (until,),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return seconds

    async def acquire(self, bulk: bool = False) -> None:
        """Attend sa place. La lecture du compteur est courte (un fichier local) : elle se fait sans quitter la boucle."""
        while True:
            wait = self.try_acquire(bulk)
            if wait <= 0.0:
                return
            await self._sleep(wait + random.uniform(0.0, self._jitter))

    def event_hooks(self) -> dict[str, list[Callable[..., Awaitable[None]]]]:
        """Crochets httpx : attendre sa place avant chaque requête vers le portail, retenir un 429 reçu."""

        async def before(request: httpx.Request) -> None:
            if is_meteofrance(request.url.host):
                await self.acquire(bulk=is_bulk(request.url.path))

        async def after(response: httpx.Response) -> None:
            if response.status_code == 429 and is_meteofrance(response.request.url.host):
                raw = response.headers.get("retry-after", "")
                seconds = self.block(float(raw) if raw.strip().isdigit() else self.default_cooldown)
                log.warning("Météo-France : 429, plus aucune requête pendant %.0f s (%s)", seconds, response.request.url.path)

        return {"request": [before], "response": [after]}
