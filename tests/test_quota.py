"""Limiteur commun du quota Météo-France : fenêtre glissante partagée entre processus, places réservées, 429 qui fait attendre.

Aucun réseau : transports simulés, temps simulé (horloge et sommeil injectés) et, pour le partage, de vrais processus
(`tests/quota_worker.py`) sur un fichier partagé en vrai temps avec une fenêtre courte.
"""

from __future__ import annotations

import asyncio
import heapq
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.main import make_http_client
from app.quota import SharedQuota, is_bulk, is_meteofrance

PORTAL = "https://public-api.meteofrance.fr/public/DPRadar/v1/mosaiques/METROPOLE/observations/LAME_D_EAU"


class Clock:
    """Temps simulé : `sleep` avance l'horloge, rien n'attend pour de vrai."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def quota_on(tmp_path: Path, clock: Clock, **kwargs: object) -> SharedQuota:
    params: dict[str, object] = {"limit": 45, "reserve": 15, "min_gap": 0.0, "jitter": 0.0}
    params.update(kwargs)
    return SharedQuota(str(tmp_path / "quota.db"), clock=clock, sleep=clock.sleep, **params)  # type: ignore[arg-type]


def max_in_any_window(stamps: list[float], window: float) -> int:
    """Le plus grand nombre de places prises dans un intervalle [t, t + fenêtre[ commençant à l'une d'elles."""
    ordered = sorted(stamps)
    return max(sum(1 for u in ordered if t <= u < t + window) for t in ordered)


# ------------------------------------------------------------------------------------------------ la fenêtre


def test_the_window_holds_the_limit_then_makes_you_wait_for_the_oldest_to_leave(tmp_path: Path) -> None:
    clock = Clock()
    quota = quota_on(tmp_path, clock, limit=5, reserve=0)
    for _ in range(5):  # cinq places, une par seconde : t, t+1, ... t+4
        assert quota.try_acquire() == 0.0
        clock.now += 1.0
    assert quota.try_acquire() == pytest.approx(55.0)  # la plus vieille (prise à t) sort à t+60, il est t+5
    clock.now += 54.0
    assert quota.try_acquire() == pytest.approx(1.0)
    clock.now += 1.0
    assert quota.try_acquire() == 0.0  # une seule place est sortie : une seule de libre
    assert quota.try_acquire() == pytest.approx(1.0)  # la suivante (prise à t+1) sort à t+61


def test_a_burst_is_spread_by_the_minimum_gap(tmp_path: Path) -> None:
    clock = Clock()
    quota = quota_on(tmp_path, clock, min_gap=0.25)
    assert quota.try_acquire() == 0.0
    assert quota.try_acquire() == pytest.approx(0.25)
    clock.now += 0.25
    assert quota.try_acquire() == 0.0


def test_bulk_products_leave_the_reserved_places_to_the_others(tmp_path: Path) -> None:
    clock = Clock()
    quota = quota_on(tmp_path, clock, limit=10, reserve=4)
    assert all(quota.try_acquire(bulk=True) == 0.0 for _ in range(6))
    assert quota.try_acquire(bulk=True) > 0.0  # les 4 dernières places ne sont pas pour lui
    assert all(quota.try_acquire() == 0.0 for _ in range(4))  # PIAF et le radar, eux, les ont
    assert quota.try_acquire() > 0.0


def test_which_requests_are_counted_and_which_are_bulk() -> None:
    assert is_meteofrance("public-api.meteofrance.fr") and is_meteofrance("api.meteofrance.fr")
    assert not is_meteofrance("open-meteo.local") and not is_meteofrance("meteofrance.fr.example.com")
    assert is_bulk("/public/aromepi/1.0/wcs/X/GetCoverage") and is_bulk("/public/pearome/1.0/wcs/X/GetCoverage")
    assert is_bulk("/public/pearpege/1.0/wcs/X/GetCoverage") and is_bulk("/previnum/DPPaquetAROME-OM/v1/models")
    assert not is_bulk("/pro/piaf/1.0/wcs/X/GetCoverage") and not is_bulk("/public/DPRadar/v1/mosaiques")


# ------------------------------------------------------------------------------------------------ un 429


def make_client(quota: SharedQuota, handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks=quota.event_hooks())


async def test_a_429_makes_every_later_request_wait_for_the_delay(tmp_path: Path) -> None:
    clock = Clock()
    quota = quota_on(tmp_path, clock)
    answers = [httpx.Response(429, headers={"Retry-After": "20"}), httpx.Response(200), httpx.Response(200)]
    sent_at: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_at.append(clock.now)
        return answers.pop(0)

    async with make_client(quota, handler) as client:
        first = await client.get(PORTAL)
        assert first.status_code == 429
        t0 = clock.now
        # Un autre produit, tout de suite après : il attend la fin du délai au lieu de repartir
        second = await client.get("https://public-api.meteofrance.fr/pro/piaf/1.0/wcs/X/GetCoverage")
        assert second.status_code == 200 and sent_at[1] - t0 == pytest.approx(20.0, abs=0.01)
        # Et la requête suivante repart normalement
        await client.get(PORTAL)
        assert sent_at[2] - sent_at[1] < 1.0
    assert sum(clock.slept) == pytest.approx(20.0, abs=0.01)  # attendu une fois, pas de boucle de petites attentes


async def test_a_429_without_retry_after_waits_the_default_and_a_huge_one_is_capped(tmp_path: Path) -> None:
    clock = Clock()
    quota = quota_on(tmp_path, clock)
    assert quota.block(10_000) == 120.0
    assert quota.block(0) == 1.0
    (tmp_path / "b").mkdir()
    quota2 = quota_on(tmp_path / "b", clock)  # un autre fichier
    answers = [httpx.Response(429), httpx.Response(200)]
    sent_at: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_at.append(clock.now)
        return answers.pop(0)

    async with make_client(quota2, handler) as client:
        await client.get(PORTAL)
        await client.get(PORTAL)
    assert sent_at[1] - sent_at[0] == pytest.approx(30.0, abs=0.01)


async def test_other_hosts_are_neither_counted_nor_delayed(tmp_path: Path) -> None:
    clock = Clock()
    quota = quota_on(tmp_path, clock, limit=2, reserve=0)
    async with make_client(quota, lambda r: httpx.Response(200)) as client:
        for _ in range(10):
            await client.get("http://open-meteo.local:8080/v1/meteofrance")
    assert clock.slept == [] and quota.try_acquire() == 0.0  # rien n'a été pris sur le quota


def test_the_gateway_client_is_wired_to_the_limiter_unless_it_is_off(tmp_path: Path) -> None:
    on, quota = make_http_client(Settings(METEO_DB_PATH=str(tmp_path / "c.db")))
    assert quota is not None and on.event_hooks["request"] and on.event_hooks["response"]
    assert (tmp_path / "meteofrance_quota.db").exists()
    assert quota.limit == 45 and quota.bulk_limit == 30
    quota.close()
    off, none = make_http_client(Settings(METEO_DB_PATH=str(tmp_path / "d.db"), METEOFRANCE_QUOTA_PER_MINUTE=0))
    assert none is None and not off.event_hooks["request"]


# ------------------------------------------------------------------------------------------------ le démarrage


class VirtualTime:
    """Un ordonnanceur de temps simulé pour asyncio : quand toutes les tâches dorment, l'heure saute au prochain réveil."""

    def __init__(self) -> None:
        self.now = 5_000_000.0
        self._wakeups: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._wakeups, (self.now + max(seconds, 0.0), self._seq, future))
        await future

    async def run(self, *coroutines) -> None:  # type: ignore[no-untyped-def]
        tasks = [asyncio.ensure_future(c) for c in coroutines]
        while not all(t.done() for t in tasks):
            for _ in range(30):  # laisser chaque tâche avancer jusqu'à son prochain sommeil
                await asyncio.sleep(0)
            if all(t.done() for t in tasks):
                break
            when, _, future = heapq.heappop(self._wakeups)
            self.now = max(self.now, when)
            future.set_result(None)
        for t in tasks:
            t.result()


async def simulate_startup(tmp_path: Path, limit: int) -> tuple[list[float], list[float]]:
    """Les collecteurs de la passerelle au démarrage (leurs plafonds propres, deux processus), 10 minutes simulées."""
    clock = VirtualTime()
    quota = SharedQuota(str(tmp_path / f"q{limit}.db"), limit=limit, reserve=15, min_gap=0.25, jitter=0.0, clock=clock, sleep=clock.sleep)
    grants: list[float] = []
    piaf_done: list[float] = []

    async def paced(per_minute: int, count: int) -> None:
        for _ in range(count):
            await quota.acquire(bulk=True)
            grants.append(clock.now)
            await clock.sleep(60.0 / per_minute)

    async def piaf(at: float) -> None:
        await clock.sleep(at)
        begin = clock.now
        sem = asyncio.Semaphore(4)

        async def step() -> None:
            async with sem:
                await quota.acquire()
                grants.append(clock.now)
                await clock.sleep(0.5)  # le téléchargement d'une carte

        await asyncio.gather(*(step() for _ in range(18)))
        piaf_done.append(clock.now - begin)

    start = clock.now
    # Les plafonds de chaque produit, comptés deux fois (deux processus) : 2 x (20 + 12 + 10 + 10) = 104 requêtes par minute demandées
    await clock.run(
        *[paced(20, 200) for _ in range(2)],
        *[paced(12, 120) for _ in range(2)],
        *[paced(10, 100) for _ in range(2)],
        *[paced(10, 100) for _ in range(2)],
        piaf(0.0),
        piaf(300.0),
    )
    window_end = start + 600.0
    kept = [g for g in grants if g < window_end]
    return kept, piaf_done


async def test_startup_never_exceeds_the_limit_in_any_minute_and_piaf_still_gets_through(tmp_path: Path) -> None:
    without, _ = await simulate_startup(tmp_path, limit=10_000)
    assert max_in_any_window(without, 60.0) > 50  # sans limiteur, la demande dépasse le quota du portail : c'est le 429 d'aujourd'hui

    stamps, piaf_done = await simulate_startup(tmp_path, limit=45)
    assert max_in_any_window(stamps, 60.0) <= 45
    assert max(stamps) - min(stamps) > 60.0  # la demande est bien étalée dans le temps, pas jetée
    assert len(piaf_done) == 2 and max(piaf_done) < 30.0  # les 18 cartes PIAF du premier tour passent vite malgré le « en gros »


# ------------------------------------------------------------------------------------------------ deux processus réels


def run_workers(tmp_path: Path, processes: int, count: int, limit: int, window: float) -> list[float]:
    path = tmp_path / "shared.db"
    SharedQuota(str(path), limit=limit, reserve=0, window=window, min_gap=0.0).close()  # crée le fichier avant la course
    workers = [
        subprocess.Popen(
            [sys.executable, "-m", "tests.quota_worker", str(path), str(count), str(limit), str(window)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(processes)
    ]
    stamps: list[float] = []
    for worker in workers:
        out, err = worker.communicate(timeout=60)
        assert worker.returncode == 0, err
        stamps.extend(float(line) for line in out.split())
    return stamps


def test_three_real_processes_share_one_limit_on_a_sliding_window(tmp_path: Path) -> None:
    limit, window = 6, 0.4
    stamps = run_workers(tmp_path, processes=3, count=14, limit=limit, window=window)
    assert len(stamps) == 42  # chacun a fini : personne n'est resté bloqué
    assert max_in_any_window(stamps, window) <= limit
    assert max(stamps) - min(stamps) >= (42 / limit - 1) * window * 0.9  # et le débit n'a pas dépassé le plafond non plus
