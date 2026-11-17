"""PIAF lu par carte : le décodeur GRIB2, la carte en mémoire, son rafraîchissement et les points hors de la carte.

Le décodeur a été comparé le 17/11/2026 à la même échéance réelle en GeoTIFF : identique sur les 1 735 201 nœuds, et
aux réponses point par point du WCS sur 6 nœuds où il pleuvait. Ces tests en fixent le comportement sans réseau.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest

from app.cache import CacheStore
from app.collectors.grib2 import decode_latlon_simple
from app.collectors.meteofrance import MeteoFranceNowcastCollector
from app.config import Settings
from tests.piaf_grib import PIAF_NI, PIAF_NJ, encode, piaf_filled

NOW = datetime(2026, 11, 17, 21, 3, 0, tzinfo=UTC)  # dernier run candidat : 20 h 50


# ------------------------------------------------------------------------------------------------ décodeur


def test_decoder_reads_the_piaf_geometry_and_values() -> None:
    values = np.zeros((PIAF_NJ, PIAF_NI))
    values[0, 0] = 1.25  # premier nœud : 51,5° N, -6° E (le message va du nord vers le sud)
    values[PIAF_NJ - 1, PIAF_NI - 1] = 2.5  # dernier : 41° N, 10,5° E
    grid = decode_latlon_simple(encode(values))
    assert grid.values.shape == (PIAF_NJ, PIAF_NI)
    assert (grid.lat0, grid.dlat, grid.dlon) == (pytest.approx(41.0), pytest.approx(0.01), pytest.approx(0.01))
    j, i = grid.index(51.5, -6.0)
    assert float(grid.values[j, i]) == pytest.approx(1.25)
    j, i = grid.index(41.0, 10.5)
    assert float(grid.values[j, i]) == pytest.approx(2.5)


def test_decoder_index_snaps_to_the_nearest_node_and_knows_its_edges() -> None:
    grid = decode_latlon_simple(piaf_filled(0.0))
    assert grid.index(43.6712, -1.4288) == grid.index(43.67, -1.43)
    assert grid.index(41.0 - 0.004, 2.0) is not None  # moins d'un demi-pas au-delà du bord : le nœud du bord
    assert grid.index(41.0 - 0.006, 2.0) is None
    assert grid.index(43.0, -6.004) is not None
    assert grid.index(43.0, -6.006) is None
    assert grid.index(40.0, 2.0) is None and grid.index(43.0, 11.0) is None
    assert grid.index(64.1, -21.9) is None  # Reykjavik


def test_decoder_south_to_north_and_other_bit_widths() -> None:
    values = np.arange(12, dtype=np.float64).reshape(3, 4) / 10  # 0,0 … 1,1
    grid = decode_latlon_simple(encode(values, la1=40.0, lo1=0.0, step=0.5, north_to_south=False, decimal=1, nbits=7))
    assert grid.lat0 == pytest.approx(40.0) and grid.lon0 == pytest.approx(0.0)
    np.testing.assert_allclose(grid.values, values, atol=1e-6)


def test_decoder_refuses_what_it_does_not_handle() -> None:
    good = piaf_filled(0.0)
    with pytest.raises(ValueError):
        decode_latlon_simple(b"<mw:fault/>")
    with pytest.raises(ValueError):
        decode_latlon_simple(good[:-10])  # tronqué
    other_packing = bytearray(good)
    s5 = 16 + 21 + 72 + 9  # sections 0, 1, 3, 4
    other_packing[s5 + 9 : s5 + 11] = (40).to_bytes(2, "big")  # modèle 5.40 (JPEG 2000)
    with pytest.raises(ValueError, match="empaquetage"):
        decode_latlon_simple(bytes(other_packing))


# ------------------------------------------------------------------------------------------------ la carte en mémoire


class FakeWcs:
    """Le WCS PIAF : une carte par échéance ; `rain` = cumul partout à +10 min ; `published` = runs publiés."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.rain = 0.3
        self.fail = False
        self.published: set[str] | None = None  # None : tous

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(502)
        coverage = request.url.params["coverageid"]
        if self.published is not None and not any(run in coverage for run in self.published):
            return httpx.Response(404, text="<mw:fault/>")
        step = int(next(s for s in request.url.params.get_list("subset") if s.startswith("time("))[5:-1])
        return httpx.Response(200, content=piaf_filled(self.rain if step == 600 else 0.0))


def make_collector(tmp_path: Path, wcs: FakeWcs, **settings: object) -> MeteoFranceNowcastCollector:
    cfg = Settings(
        METEO_DB_PATH=str(tmp_path / "cache.db"),
        METEOFRANCE_API_KEY="secret-mf-token-123",
        OPEN_METEO_BASE_URL="http://open-meteo.local:8080",
        METEO_PIAF_STEPS=6,
        **settings,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(wcs))
    return MeteoFranceNowcastCollector(client, CacheStore(cfg.db_path), cfg)


async def test_many_points_cost_one_map_per_run(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs)
    points = [(48.85 + k * 0.03, 2.35 - k * 0.05) for k in range(40)]
    blocks = await asyncio.gather(*(collector.get_rain_nowcast(lat, lon, now=NOW) for lat, lon in points))
    assert all(b.source_model == "meteofrance-piaf" for b, _ in blocks)
    assert all([s.rain_level for s in b.slices] == [1, 3, 1, 1, 1, 1] for b, _ in blocks)
    assert len(wcs.requests) == 6  # 6 échéances, une fois, pour 40 points demandés en même temps
    assert {r.url.params["format"] for r in wcs.requests} == {"application/wmo-grib"}


async def test_a_point_outside_the_map_is_not_a_failure(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs, METEO_CIRCUIT_FAIL_THRESHOLD=2)
    for _ in range(5):
        block, status = await collector.get_rain_nowcast(64.1, -21.9, now=NOW)  # Reykjavik
        assert block.status == "unavailable" and block.slices == []
        assert status.last_error == "hors de la zone PIAF"
    assert not collector._cache.is_circuit_open(collector.provider_name)
    block, _ = await collector.get_rain_nowcast(48.85, 2.35, now=NOW)
    assert block.source_model == "meteofrance-piaf"
    assert len(wcs.requests) == 6


async def test_an_old_map_is_served_while_the_next_run_downloads(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs)
    wcs.published = {"2026-11-17T20.50.00Z"}
    await collector.get_rain_nowcast(48.85, 2.35, now=NOW)
    assert collector._run is not None and collector._run.run.strftime("%H:%M") == "20:50"

    # 6 minutes plus tard : la carte a passé son délai (5 min) ; le run de 20 h 55 est publié, avec plus de pluie.
    collector._run.loaded_at = time.time() - 360
    wcs.published = {"2026-11-17T20.55.00Z"}
    wcs.rain = 0.6
    later = NOW.replace(minute=8)
    block, _ = await collector.get_rain_nowcast(45.76, 4.84, now=later)
    assert [s.rain_level for s in block.slices][:2] == [1, 3]  # tout de suite : l'ancienne carte
    await collector._refresh
    assert collector._run.run.strftime("%H:%M") == "20:55"
    block, _ = await collector.get_rain_nowcast(43.3, 5.37, now=later)
    assert [s.rain_level for s in block.slices][:2] == [1, 4]  # la nouvelle : 0,6 mm en 5 min = 7,2 mm/h
    assert len(wcs.requests) == 6 + 6  # un run, puis le suivant : 6 échéances chacun


async def test_no_newer_run_means_no_download(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs)
    wcs.published = {"2026-11-17T20.50.00Z"}
    await collector.get_rain_nowcast(48.85, 2.35, now=NOW)
    collector._run.loaded_at = time.time() - 360
    await collector.get_rain_nowcast(45.76, 4.84, now=NOW)
    await collector._refresh
    assert len(wcs.requests) == 6  # 20 h 50 est encore le dernier run : rien de retéléchargé
    assert time.time() - collector._run.loaded_at < 5  # et on ne regarde pas de nouveau avant 5 min


async def test_a_map_too_old_is_not_served_when_the_source_fails(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs)
    await collector.get_rain_nowcast(48.85, 2.35, now=NOW)
    collector._run.loaded_at = time.time() - 3600  # au-delà de 15 min
    wcs.fail = True
    an_hour_later = NOW.replace(hour=22)  # des runs plus récents existent, et le WCS est en panne
    block, status = await collector.get_rain_nowcast(
        45.76, 4.84, now=an_hour_later
    )  # point jamais servi : pas de cache
    assert block.slices == [] and status.status == "unavailable"
    assert status.last_error is not None and "502" in status.last_error
