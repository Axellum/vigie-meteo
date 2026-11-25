"""Cache mémoire des tuiles : borne en octets, clé complète, aucune tuile d'un ancien run ou d'une ancienne image servie.

Aucun appel réseau : les grilles PIAF et les images radar sont construites en mémoire.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

import app.main as main_module
from app.collectors.meteofrance import PIAF_STEP_SECONDS, PiafRun
from app.radar_grid import TRANSPARENT_TILE, RadarFrame
from app.tile_cache import ENTRY_OVERHEAD_BYTES, TileCache, piaf_key, radar_key
from tests.test_piaf_tiles import PARIS, RUN_TIME, api_client, make_piaf_grid, read_png
from tests.test_piaf_tiles import place_in_tile as piaf_place
from tests.test_radar import EPOCH, frame_with_rain_at
from tests.test_radar import api_client as radar_api_client
from tests.test_radar import place_in_tile as radar_place
from tests.test_radar import read_png as radar_read_png

# ------------------------------------------------------------------------------------------------ le cache seul


def png(n: int) -> bytes:
    return bytes(n)


def test_the_cache_is_bounded_in_bytes_and_evicts_the_least_recently_used() -> None:
    cost = 1000 + ENTRY_OVERHEAD_BYTES
    cache = TileCache(max_bytes=3 * cost)
    for name in "abc":
        cache.put(name, png(1000))
    assert len(cache) == 3 and cache.size_bytes == 3 * cost

    assert cache.get("a") is not None  # « a » devient le plus récent : « b » est le prochain évincé
    cache.put("d", png(1000))
    assert cache.get("b") is None
    assert all(cache.get(k) is not None for k in "acd")
    assert cache.size_bytes <= cache.max_bytes


def test_the_bound_is_in_bytes_not_in_entries() -> None:
    cache = TileCache(max_bytes=10_000)
    for i in range(50):  # des tuiles de tailles différentes : la somme ne dépasse jamais la borne
        cache.put(i, png(100 + 90 * i))
        assert cache.size_bytes <= 10_000
    kept = [cache.get(k) for k in range(50)]
    assert cache.size_bytes == sum(len(v) + ENTRY_OVERHEAD_BYTES for v in kept if v is not None)
    assert kept[-1] is not None and kept[0] is None  # les plus anciennes sont parties
    big = png(9_000)
    cache.put("big", big)  # une grosse tuile en évince plusieurs petites
    assert cache.get("big") == big and cache.size_bytes <= 10_000


def test_replacing_a_key_does_not_leak_bytes_and_an_oversized_tile_is_not_kept() -> None:
    cache = TileCache(max_bytes=5_000)
    cache.put("k", png(1_000))
    cache.put("k", png(2_000))
    assert len(cache) == 1 and cache.size_bytes == 2_000 + ENTRY_OVERHEAD_BYTES
    cache.put("huge", png(6_000))
    assert cache.get("huge") is None and len(cache) == 1


def test_a_cache_of_zero_bytes_is_off() -> None:
    cache = TileCache(max_bytes=0)
    cache.put("k", png(10))
    assert cache.get("k") is None and len(cache) == 0 and cache.size_bytes == 0


def test_the_key_holds_everything_that_decides_the_image() -> None:
    base = piaf_key(1000, 1300, PIAF_STEP_SECONDS, 8, 130, 90)
    variants = [
        piaf_key(1300, 1300, PIAF_STEP_SECONDS, 8, 130, 90),  # un autre run
        piaf_key(1000, 1600, PIAF_STEP_SECONDS, 8, 130, 90),  # une autre échéance
        piaf_key(1000, 1300, 600, 8, 130, 90),  # un autre pas
        piaf_key(1000, 1300, PIAF_STEP_SECONDS, 9, 130, 90),
        piaf_key(1000, 1300, PIAF_STEP_SECONDS, 8, 131, 90),
        piaf_key(1000, 1300, PIAF_STEP_SECONDS, 8, 130, 91),
    ]
    assert len({base, *variants}) == 1 + len(variants)
    radar = radar_key("france", 1300, 5, 8, 130, 90)
    others = [
        radar_key("antilles", 1300, 5, 8, 130, 90),  # une autre zone
        radar_key("france", 1600, 5, 8, 130, 90),  # une autre image
        radar_key("france", 1300, 6, 8, 130, 90),  # la même image réécrite
        radar_key("france", 1300, 5, 9, 130, 90),
    ]
    assert len({radar, *others}) == 1 + len(others)
    assert radar != piaf_key(1300, 1300, PIAF_STEP_SECONDS, 8, 130, 90)  # radar et PIAF ne se confondent pas


# ------------------------------------------------------------------------------------------------ routes PIAF


def count_piaf_renders(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    renders: list[int] = []
    real = main_module.piaf_tile_png

    def counting(grid, z, x, y):  # type: ignore[no-untyped-def]
        renders.append(1)
        return real(grid, z, x, y)

    monkeypatch.setattr(main_module, "piaf_tile_png", counting)
    return renders


def test_piaf_tile_is_drawn_once_then_served_from_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renders = count_piaf_renders(monkeypatch)
    dry = make_piaf_grid(rain_at=None)
    wet = make_piaf_grid(rain_at=PARIS, mm_5min=0.4, half=4)
    with api_client(tmp_path, []) as client:
        client.app.state.now_collector._run = PiafRun(run=RUN_TIME, steps=[dry, wet, *[dry] * 10])
        run_epoch = int(RUN_TIME.timestamp())
        wet_epoch = int((RUN_TIME + timedelta(minutes=10)).timestamp())
        tx, ty, px, py = piaf_place(*PARIS, 8)
        url = f"/v1/rain-nowcast/tiles/{wet_epoch}/8/{tx}/{ty}.png"

        first = client.get(f"{url}?run={run_epoch}")
        second = client.get(f"{url}?run={run_epoch}")
        assert len(renders) == 1 and client.app.state.tile_cache.hits == 1
        assert second.content == first.content and read_png(first.content)[py, px] == 5
        # Les en-têtes sont ceux d'avant, tuile servie du cache comprise
        for r in (first, second):
            assert r.headers["cache-control"] == "public, max-age=3600, immutable"
            assert r.headers["x-piaf-run"] == str(run_epoch)
        # Sans `run` dans l'adresse : la même image du cache, mais gardée 60 s seulement
        other = client.get(url)
        assert len(renders) == 1 and other.content == first.content
        assert other.headers["cache-control"] == "public, max-age=60" and other.headers["x-piaf-run"] == str(run_epoch)


def test_a_newer_piaf_run_never_serves_the_tile_of_the_older_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renders = count_piaf_renders(monkeypatch)
    dry = make_piaf_grid(rain_at=None)
    wet = make_piaf_grid(rain_at=PARIS, mm_5min=0.4, half=4)
    with api_client(tmp_path, []) as client:
        collector = client.app.state.now_collector
        # Run de 12 h : son échéance de 12 h 10 est pluvieuse.
        collector._run = PiafRun(run=RUN_TIME, steps=[dry, wet, *[dry] * 10])
        epoch = int((RUN_TIME + timedelta(minutes=10)).timestamp())
        tx, ty, px, py = piaf_place(*PARIS, 8)
        url = f"/v1/rain-nowcast/tiles/{epoch}/8/{tx}/{ty}.png"
        assert read_png(client.get(url).content)[py, px] == 5

        # Le run de 12 h 05 redessine le même instant (12 h 10 = son échéance +5 min) : sec.
        newer = RUN_TIME + timedelta(minutes=5)
        collector._run = PiafRun(run=newer, steps=[dry] * 12)
        r_new = client.get(f"{url}?run={int(newer.timestamp())}")
        assert r_new.content == TRANSPARENT_TILE and r_new.headers["x-piaf-run"] == str(int(newer.timestamp()))
        assert len(renders) == 2  # redessinée, pas relue dans le cache de l'ancien run

        # Le deuxième processus tient encore l'ancien run : il sert la sienne, pas celle du nouveau.
        collector._run = PiafRun(run=RUN_TIME, steps=[dry, wet, *[dry] * 10])
        again = client.get(url)
        assert read_png(again.content)[py, px] == 5 and again.headers["x-piaf-run"] == str(int(RUN_TIME.timestamp()))


def test_the_cache_off_draws_every_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renders = count_piaf_renders(monkeypatch)
    wet = make_piaf_grid(rain_at=PARIS, mm_5min=0.4, half=4)
    with api_client(tmp_path, [], METEO_TILE_CACHE_MB=0) as client:
        client.app.state.now_collector._run = PiafRun(run=RUN_TIME, steps=[wet] * 12)
        tx, ty, _, _ = piaf_place(*PARIS, 8)
        epoch = int((RUN_TIME + timedelta(minutes=5)).timestamp())
        for _ in range(2):
            assert client.get(f"/v1/rain-nowcast/tiles/{epoch}/8/{tx}/{ty}.png").status_code == 200
        assert len(renders) == 2


# ------------------------------------------------------------------------------------------------ routes radar


def count_radar_renders(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    renders: list[int] = []
    real = main_module.tile_png

    def counting(frame, z, x, y):  # type: ignore[no-untyped-def]
        renders.append(1)
        return real(frame, z, x, y)

    monkeypatch.setattr(main_module, "tile_png", counting)
    return renders


def test_radar_tile_is_drawn_once_then_served_from_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renders = count_radar_renders(monkeypatch)
    with radar_api_client(tmp_path) as client:
        client.app.state.radar.store.save(frame_with_rain_at(*PARIS, half=12))
        x, y, px, py = radar_place(*PARIS, 8)
        first = client.get(f"/v1/radar/tiles/{EPOCH}/8/{x}/{y}.png")
        second = client.get(f"/v1/radar/tiles/{EPOCH}/8/{x}/{y}.png")
        assert len(renders) == 1 and client.app.state.tile_cache.hits == 1
        assert second.content == first.content and radar_read_png(first.content)[py, px] == 5
        for r in (first, second):
            assert r.headers["cache-control"] == "public, max-age=86400, immutable"
        # La même adresse sur une zone sans image ne reprend pas la tuile de la métropole
        assert client.get(f"/v1/radar/antilles/tiles/{EPOCH}/8/{x}/{y}.png").status_code == 404


def test_a_new_radar_image_or_a_rewritten_one_never_serves_the_old_tile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    renders = count_radar_renders(monkeypatch)
    with radar_api_client(tmp_path) as client:
        store = client.app.state.radar.store
        wet = frame_with_rain_at(*PARIS, half=12)
        store.save(wet)
        x, y, px, py = radar_place(*PARIS, 8)
        url = f"/v1/radar/tiles/{EPOCH}/8/{x}/{y}.png"
        assert radar_read_png(client.get(url).content)[py, px] == 5

        # Une image plus récente : une autre adresse, donc une autre clé
        store.save(RadarFrame(end=wet.end + timedelta(minutes=5), classes=np.zeros_like(wet.classes)))
        later = client.get(f"/v1/radar/tiles/{EPOCH + 300}/8/{x}/{y}.png")
        assert later.content == TRANSPARENT_TILE and len(renders) == 2

        # La même image réécrite (autre contenu, fichier plus récent) : la date du fichier change la clé
        store.save(frame_with_rain_at(*PARIS, cls=7, half=12))
        path = store._path(EPOCH)
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000_000))
        store._memory.clear()
        assert radar_read_png(client.get(url).content)[py, px] == 7
        assert len(renders) == 3


def test_a_purged_radar_image_is_a_404_even_if_its_tile_was_cached(tmp_path: Path) -> None:
    with radar_api_client(tmp_path) as client:
        store = client.app.state.radar.store
        store.save(frame_with_rain_at(*PARIS, half=12))
        x, y, _, _ = radar_place(*PARIS, 8)
        url = f"/v1/radar/tiles/{EPOCH}/8/{x}/{y}.png"
        assert client.get(url).status_code == 200
        store.purge(now=EPOCH + 10 * 86400)
        assert client.get(url).status_code == 404
