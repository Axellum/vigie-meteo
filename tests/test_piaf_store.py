"""Le run PIAF partagé entre les deux processus de la passerelle : un seul téléchargement par run (sans réseau)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.cache import CacheStore
from app.collectors.meteofrance import MeteoFranceNowcastCollector
from app.config import Settings
from app.piaf_store import PiafStore
from tests.test_piaf_grid import NOW, FakeWcs

ROOT = Path(__file__).resolve().parent.parent


def make_worker(tmp_path: Path, wcs: FakeWcs, name: str) -> MeteoFranceNowcastCollector:
    """Un « processus » : sa propre base de cache et sa propre mémoire, le dossier `piaf/` partagé avec les autres."""
    cfg = Settings(
        METEO_DB_PATH=str(tmp_path / f"{name}.db"),
        METEO_PIAF_DIR=str(tmp_path / "piaf"),
        METEOFRANCE_API_KEY="secret-mf-token-123",
        METEO_PIAF_STEPS=6,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(wcs))
    return MeteoFranceNowcastCollector(client, CacheStore(cfg.db_path), cfg)


def age_everything(workers: list[MeteoFranceNowcastCollector], directory: Path, seconds: float) -> None:
    """Le temps passe : la carte de chaque processus et le fichier du disque vieillissent ensemble."""
    stamp = time.time() - seconds
    for worker in workers:
        assert worker._run is not None
        worker._run.loaded_at = stamp
    for stored in directory.glob("*.npz"):
        os.utime(stored, (stamp, stamp))


async def test_two_workers_download_a_run_once(tmp_path: Path) -> None:
    wcs = FakeWcs()
    first, second = make_worker(tmp_path, wcs, "a"), make_worker(tmp_path, wcs, "b")
    runs = await asyncio.gather(first.ensure_run(now=NOW), second.ensure_run(now=NOW))
    assert all(r is not None and r.run.strftime("%H:%M") == "20:50" for r in runs)
    assert len(wcs.requests) == 6  # 6 échéances pour les deux, pas 12
    # Ce que l'autre a lu sur disque est identique à ce qui a été téléchargé.
    assert runs[0] is not runs[1]
    for left, right in zip(runs[0].steps, runs[1].steps, strict=True):
        np.testing.assert_array_equal(left.values, right.values)
        assert (left.lat0, left.lon0, left.dlat, left.dlon) == (right.lat0, right.lon0, right.dlat, right.dlon)
    assert runs[0].point(48.85, 2.35) == runs[1].point(48.85, 2.35)


async def test_the_second_worker_reads_the_next_run_from_disk(tmp_path: Path) -> None:
    wcs = FakeWcs()
    first, second = make_worker(tmp_path, wcs, "a"), make_worker(tmp_path, wcs, "b")
    wcs.published = {"2026-11-17T20.50.00Z"}
    await asyncio.gather(first.ensure_run(now=NOW), second.ensure_run(now=NOW))
    assert len(wcs.requests) == 6

    # Le run de 20 h 55 est publié ; les deux cartes ont passé leur délai : l'un le télécharge, l'autre le relit.
    wcs.published = {"2026-11-17T20.55.00Z"}
    wcs.rain = 0.6
    age_everything([first, second], tmp_path / "piaf", 1000)  # au-delà de 15 min : on attend le chargement
    later = NOW.replace(minute=8)
    await asyncio.gather(first.ensure_run(now=later), second.ensure_run(now=later))
    assert first._run is not None and second._run is not None
    assert first._run.run.strftime("%H:%M") == second._run.run.strftime("%H:%M") == "20:55"
    assert len(wcs.requests) == 6 + 6  # un run de plus pour les deux
    assert second._run.point(48.85, 2.35) == first._run.point(48.85, 2.35)


async def test_no_newer_run_is_looked_for_by_one_worker_only(tmp_path: Path) -> None:
    wcs = FakeWcs()
    first, second = make_worker(tmp_path, wcs, "a"), make_worker(tmp_path, wcs, "b")
    wcs.published = {"2026-11-17T20.50.00Z"}
    await asyncio.gather(first.ensure_run(now=NOW), second.ensure_run(now=NOW))

    # Le délai est passé mais 20 h 50 reste le dernier run publié : le premier qui regarde le constate, l'autre le croit.
    wcs.requests.clear()
    age_everything([first, second], tmp_path / "piaf", 1000)  # au-delà de 15 min : on attend le chargement
    later = NOW.replace(minute=7)  # candidats 20 h 55 puis 20 h 50 : seul le premier est demandé (404)
    await asyncio.gather(first.ensure_run(now=later), second.ensure_run(now=later))
    assert len(wcs.requests) == 1
    assert time.time() - first._run.loaded_at < 5 and time.time() - second._run.loaded_at < 5


async def test_a_worker_started_late_reads_the_disk_without_a_request(tmp_path: Path) -> None:
    wcs = FakeWcs()
    wcs.published = {"2026-11-17T20.50.00Z"}
    await make_worker(tmp_path, wcs, "a").ensure_run(now=NOW)
    assert len(wcs.requests) == 6
    late = make_worker(tmp_path, wcs, "b")  # redémarrage d'un processus : rien en mémoire
    run = await late.ensure_run(now=NOW)
    assert run is not None and run.run.strftime("%H:%M") == "20:50"
    assert len(wcs.requests) == 6


async def test_a_corrupt_or_short_file_is_ignored_and_the_run_downloaded(tmp_path: Path) -> None:
    wcs = FakeWcs()
    directory = tmp_path / "piaf"
    directory.mkdir()
    epoch = int(NOW.replace(minute=50).timestamp())
    (directory / f"{epoch}.npz").write_bytes(b"pas une archive")
    run = await make_worker(tmp_path, wcs, "a").ensure_run(now=NOW)
    assert run is not None and len(wcs.requests) == 6

    # Un run rangé avec moins d'échéances que `METEO_PIAF_STEPS` n'est pas repris.
    store = PiafStore(tmp_path / "autre")
    store.save(run.run, run.steps[:3])
    assert store.load(int(run.run.timestamp()), 6) is None
    assert store.load(int(run.run.timestamp()), 3) is not None


async def test_the_store_keeps_the_last_runs_only(tmp_path: Path) -> None:
    wcs = FakeWcs()
    run = await make_worker(tmp_path, wcs, "a").ensure_run(now=NOW)
    assert run is not None
    store = PiafStore(tmp_path / "garde", keep_runs=2)
    for k in range(5):
        store.save(run.run.replace(minute=k * 5), run.steps[:1])
    assert len(store.epochs()) == 5
    assert store.purge() == 3
    assert len(store.epochs()) == 2
    newest = store.newest()
    assert newest is not None and newest[0] == store.epochs()[-1]


async def test_an_unusable_directory_does_not_stop_the_gateway(tmp_path: Path) -> None:
    wcs = FakeWcs()
    (tmp_path / "piaf").write_text("un fichier là où il faudrait un dossier")
    run = await make_worker(tmp_path, wcs, "a").ensure_run(now=NOW)
    assert run is not None and run.run.strftime("%H:%M") == "20:50"
    assert len(wcs.requests) == 6


async def test_the_lock_wait_is_bounded(tmp_path: Path) -> None:
    store = PiafStore(tmp_path / "piaf", lock_timeout=0.4)
    other = PiafStore(tmp_path / "piaf", lock_timeout=0.4)
    async with store.exclusive() as held:
        assert held is True
        started = time.monotonic()
        async with other.exclusive() as also:
            assert also is False  # l'autre « processus » tient le verrou : attente bornée, puis on continue sans
        assert 0.3 < time.monotonic() - started < 3.0
    async with other.exclusive() as freed:
        assert freed is True


def test_two_real_processes_download_a_run_once(tmp_path: Path) -> None:
    """Deux vrais processus Python lancés en même temps : le compteur partagé compte les requêtes PIAF envoyées."""
    counters = [tmp_path / "un.txt", tmp_path / "deux.txt"]
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "tests.piaf_worker", str(tmp_path), str(counter)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for counter in counters
    ]
    outputs = [proc.communicate(timeout=120) for proc in procs]
    for proc, (out, err) in zip(procs, outputs, strict=True):
        assert proc.returncode == 0, err
        assert out.strip().startswith("2026-11-17T20:50:00")
    sent = sum(len(c.read_text(encoding="utf-8").splitlines()) for c in counters if c.exists())
    assert sent == 6  # 6 échéances pour les deux processus, contre 12 sans partage


@pytest.mark.parametrize("seconds", [0, 120])
async def test_a_fresh_disk_run_is_taken_as_is(tmp_path: Path, seconds: int) -> None:
    wcs = FakeWcs()
    wcs.published = {"2026-11-17T20.50.00Z"}
    first = make_worker(tmp_path, wcs, "a")
    await first.ensure_run(now=NOW)
    age_everything([first], tmp_path / "piaf", seconds)
    wcs.requests.clear()
    run = await make_worker(tmp_path, wcs, "b").ensure_run(now=NOW)
    assert run is not None and wcs.requests == []
