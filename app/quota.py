"""Limiteur commun du quota du portail Météo-France : une clé, 50 requêtes par minute, tous produits et tous processus confondus.

Chaque collecteur a son propre rythme (`METEO_*_REQUESTS_PER_MINUTE`), mais la somme de ces plafonds dépasse 50 et les deux
processus de la passerelle comptent chacun de leur côté : au démarrage, tout part en même temps et le portail répond 429.
Ici, une seule comptabilité, partagée par les processus dans un petit fichier SQLite à côté de la base du cache :

- une fenêtre glissante de `limit` requêtes sur `window` secondes (45 sur 60 : une marge sous les 50 du portail) ;
- les produits « en gros » (cartes de prévision d'ensemble, AROME-PI, AROME-OM : des centaines de requêtes par run) ne
  peuvent prendre que `limit - reserve` des places ; le reste est gardé pour la pluie PIAF, le radar, DPClim et la
  météo des forêts, qu'un visiteur attend ;
- un petit écart minimal entre deux requêtes (`min_gap`), pour que le premier tour ne parte pas en rafale ;
- après un 429, plus aucune requête ne part **vers la même API** (`/public/pearome/...` : `pearome`) avant la fin du délai
  demandé (`Retry-After`, sinon 30 s doublées à chaque refus de suite, 120 s au plus) : tous les processus attendent, personne
  ne reboucle sur un refus, et la pluie PIAF ou le radar ne paient pas le refus d'une prévision d'ensemble (vu le 25/11/2026 :
  47 refus en 2 h 30, tous PE-AROME ou PE-ARPEGE, sans `Retry-After`, et chacun arrêtait tout 30 s).

Chaque 429 est écrit au journal avec le nombre de requêtes parties dans la fenêtre (toutes, et vers cette API) et les en-têtes
de limite du portail s'il en donne : de quoi trouver la vraie limite d'une API.

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
LIMIT_HEADER_WORDS = ("ratelimit", "rate-limit", "retry-after", "quota", "throttl")  # en-têtes de limite recopiés au journal


def is_meteofrance(host: str) -> bool:
    return host == HOST_SUFFIX or host.endswith("." + HOST_SUFFIX)


def is_bulk(path: str) -> bool:
    return any(marker in path for marker in BULK_PATH_MARKERS)


def api_of(path: str) -> str:
    """L'API du portail visée : le segment après l'accès (`/public/pearome/1.0/...` → `pearome`, `/pro/piaf/1.0/...` → `piaf`)."""
    parts = [part for part in path.split("/") if part]
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else "")


def limit_headers(headers: httpx.Headers) -> str:
    """Les en-têtes de limite d'une réponse (`X-RateLimit-*`, `Retry-After`...), pour le journal ; vide s'il n'y en a pas."""
    found = [f"{name}={value[:80]}" for name, value in headers.items() if any(word in name.lower() for word in LIMIT_HEADER_WORDS)]
    return ", ".join(found)


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
        self._conn.execute("CREATE TABLE IF NOT EXISTS grants (ts REAL NOT NULL, api TEXT NOT NULL DEFAULT '')")
        self._conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value REAL NOT NULL)")
        if not self._has_api_column():  # un fichier d'avant le blocage par API
            try:
                self._conn.execute("ALTER TABLE grants ADD COLUMN api TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:  # l'autre processus l'a ajoutée entre-temps
                if not self._has_api_column():
                    raise

    def _has_api_column(self) -> bool:
        return "api" in {row[1] for row in self._conn.execute("PRAGMA table_info(grants)")}

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def try_acquire(self, bulk: bool = False, api: str = "") -> float:
        """Prend une place si elle est libre (rend 0.0), sinon rend le nombre de secondes à attendre avant de réessayer.

        `api` : l'API visée (`api_of`) ; elle attend aussi la fin de son propre blocage après un 429.

        Tout se passe sous `BEGIN IMMEDIATE` : deux processus ne peuvent pas compter la même place libre. L'heure est lue sous le verrou, pour que
        les dates écrites suivent l'ordre des écritures.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                self._conn.execute("DELETE FROM grants WHERE ts <= ?", (now - self.window,))
                stamps = [row[0] for row in self._conn.execute("SELECT ts FROM grants ORDER BY ts")]
                blocked = self._conn.execute("SELECT value FROM state WHERE key = ?", (f"blocked_until:{api}",)).fetchone()
                wait = max(0.0, (blocked[0] if blocked else 0.0) - now)
                cap = self.bulk_limit if bulk else self.limit
                if len(stamps) >= cap:  # les (n - cap + 1) plus vieilles doivent sortir de la fenêtre
                    wait = max(wait, stamps[len(stamps) - cap] + self.window - now)
                if stamps:
                    wait = max(wait, stamps[-1] + self.min_gap - now)
                if wait <= 0.0:
                    self._conn.execute("INSERT INTO grants (ts, api) VALUES (?, ?)", (now, api))
                    self.last_grant_ts = now
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return wait

    def block(self, seconds: float, api: str = "") -> float:
        """Plus aucune requête vers `api` pendant `seconds` ; rend la durée retenue (1 s au moins, `MAX_COOLDOWN_S` au plus)."""
        seconds = min(max(seconds, 1.0), MAX_COOLDOWN_S)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._block_until(api, self._clock() + seconds)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return seconds

    def refused(self, api: str, retry_after: float | None = None) -> tuple[float, int]:
        """Un 429 de `api` : la bloque `Retry-After` secondes, sinon 30 s doublées à chaque refus de suite ; rend (attente, refus de suite).

        Un 429 qui arrive alors que l'API est déjà bloquée vient d'une requête partie avant le blocage (les autres attendent sa
        fin) : il ne compte pas comme un refus de plus.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                row = self._conn.execute("SELECT value FROM state WHERE key = ?", (f"blocked_until:{api}",)).fetchone()
                blocked_until = row[0] if row else 0.0
                row = self._conn.execute("SELECT value FROM state WHERE key = ?", (f"strikes:{api}",)).fetchone()
                strikes = int(row[0]) if row else 0
                if blocked_until <= now or not strikes:
                    strikes += 1
                    self._conn.execute(
                        "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (f"strikes:{api}", float(strikes)),
                    )
                wanted = retry_after if retry_after is not None else self.default_cooldown * 2 ** (strikes - 1)
                seconds = min(max(wanted, 1.0), MAX_COOLDOWN_S)
                self._block_until(api, now + seconds)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return max(seconds, blocked_until - now), strikes

    def answered(self, api: str) -> None:
        """Une réponse acceptée de `api` : ses refus de suite sont oubliés (une lecture ; une écriture seulement s'il y en avait)."""
        key = f"strikes:{api}"
        with self._lock:
            if self._conn.execute("SELECT 1 FROM state WHERE key = ?", (key,)).fetchone():
                self._conn.execute("DELETE FROM state WHERE key = ?", (key,))

    def recent(self, api: str) -> tuple[int, int]:
        """Les places prises dans la fenêtre : toutes, et celles de `api` (pour le journal d'un 429)."""
        with self._lock:
            total, mine = self._conn.execute(
                "SELECT count(*), coalesce(sum(api = ?), 0) FROM grants WHERE ts > ?", (api, self._clock() - self.window)
            ).fetchone()
        return int(total), int(mine)

    def _block_until(self, api: str, until: float) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = max(value, excluded.value)",
            (f"blocked_until:{api}", until),
        )

    async def acquire(self, bulk: bool = False, api: str = "") -> None:
        """Attend sa place. La lecture du compteur est courte (un fichier local) : elle se fait sans quitter la boucle."""
        while True:
            wait = self.try_acquire(bulk, api)
            if wait <= 0.0:
                return
            await self._sleep(wait + random.uniform(0.0, self._jitter))

    def event_hooks(self) -> dict[str, list[Callable[..., Awaitable[None]]]]:
        """Crochets httpx : attendre sa place avant chaque requête vers le portail, retenir un 429 reçu (pour son API seulement)."""

        async def before(request: httpx.Request) -> None:
            if is_meteofrance(request.url.host):
                await self.acquire(bulk=is_bulk(request.url.path), api=api_of(request.url.path))

        async def after(response: httpx.Response) -> None:
            if not is_meteofrance(response.request.url.host):
                return
            api = api_of(response.request.url.path)
            if response.status_code == 429:
                raw = response.headers.get("retry-after", "").strip()
                seconds, strikes = self.refused(api, float(raw) if raw.isdigit() else None)
                total, mine = self.recent(api)
                log.warning(
                    "Météo-France : 429 de %s (%d de suite), plus aucune requête vers cette API pendant %.0f s ; "
                    "%d requêtes parties en %.0f s, dont %d vers elle ; en-têtes de limite : %s (%s)",
                    api,
                    strikes,
                    seconds,
                    total,
                    self.window,
                    mine,
                    limit_headers(response.headers) or "aucun",
                    response.request.url.path,
                )
            elif response.status_code < 400:
                self.answered(api)

        return {"request": [before], "response": [after]}
