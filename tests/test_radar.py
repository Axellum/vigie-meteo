"""Mosaïque radar : projection, classes, tuiles PNG, images gardées, téléchargement et routes.

La projection est comparée aux quatre coins du vrai fichier du 18/11/2026 (`where/UL_lat`, `UL_lon`, …) ; le calage a
aussi été comparé à la carte PIAF le même jour (voir `app/radar_grid.py`).
"""

from __future__ import annotations

import math
import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.collectors.radar import FIRST_TRY_AFTER_S, RadarCollector, RadarStore, parse_validity
from app.config import Settings
from app.main import create_app
from app.radar_grid import (
    AREAS,
    PIXEL_M,
    SIZE,
    TILE,
    TOP_M,
    TRANSPARENT_TILE,
    RadarFrame,
    classify,
    decode_hdf5,
    pool_factor,
    project,
    tile_png,
)
from tests.radar_h5 import PROJDEF, PROJDEFS, make_h5

END = datetime(2026, 11, 18, 8, 25, tzinfo=UTC)
EPOCH = int(END.timestamp())
PARIS = (48.8566, 2.3522)


def read_png(png: bytes) -> np.ndarray:
    """Les indices de palette d'un PNG indexé de 8 bits (le seul que ce module écrit)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, body = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        assert zlib.crc32(kind + body) & 0xFFFFFFFF == struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])[0]
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
            assert body[8:10] == bytes([8, 3])
        if kind == b"IDAT":
            idat += body
        pos += 12 + length
    width, height = size
    rows = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(height, width + 1)
    assert not rows[:, 0].any()
    return rows[:, 1:]


def place_in_tile(lat: float, lon: float, z: int) -> tuple[int, int, int, int]:
    n = 2**z
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return int(fx), int(fy), int((fx % 1) * TILE), int((fy % 1) * TILE)


def frame_with_rain_at(lat: float, lon: float, cls: int = 5, half: int = 6) -> RadarFrame:
    classes = np.zeros((SIZE, SIZE), dtype=np.uint8)
    x, y = project(lat, lon)
    col, row = int(x // PIXEL_M), int((TOP_M - y) // PIXEL_M)
    classes[row - half : row + half + 1, col - half : col + half + 1] = cls
    return RadarFrame(end=END, classes=classes)


# ------------------------------------------------------------------------------------------------ projection


def test_projection_reproduces_the_four_corners_of_the_real_file() -> None:
    corners = {  # (lat, lon) lus dans `where` du fichier du 18/11/2026 -> (x, y) attendus : l'image va de 0 à 1 736 000 m
        (53.67, -9.965): (0.0, TOP_M),
        (52.54813781717718, 17.564202528305234): (SIZE * PIXEL_M, TOP_M),
        (38.14493289858148, -6.715173408021557): (0.0, 0.0),
        (37.45745953865351, 11.976054526221487): (SIZE * PIXEL_M, 0.0),
    }
    for (lat, lon), (x, y) in corners.items():
        px, py = project(lat, lon)
        assert float(px) == pytest.approx(x, abs=0.05)
        assert float(py) == pytest.approx(y, abs=0.05)


def test_north_is_up_and_east_is_right() -> None:
    x0, y0 = project(46.0, 2.0)
    assert project(47.0, 2.0)[1] > y0  # plus au nord : plus haut
    assert project(46.0, 3.0)[0] > x0  # plus à l'est : plus à droite


# ------------------------------------------------------------------------------------------------ classes et fichier


def test_classify_thresholds_nodata_and_dry() -> None:
    # une unité = 0,01 mm en 5 min = 0,12 mm/h
    raw = np.array([[0, 1, 4, 5, 9, 17, 34, 5334, 65534, 65535]], dtype=np.uint16)
    assert classify(raw).tolist() == [[0, 1, 1, 2, 3, 4, 5, 9, 0, 0]]


def test_decode_reads_time_classes_and_refuses_what_it_does_not_know() -> None:
    data = np.zeros((SIZE, SIZE), dtype=np.uint16)
    data[100, 200] = 1000  # 120 mm/h
    data[0, 0] = 65535
    frame = decode_hdf5(make_h5(data))
    assert frame.end == END and frame.epoch == EPOCH
    assert frame.classes[100, 200] == 9 and frame.classes[0, 0] == 0 and frame.classes.shape == (SIZE, SIZE)
    with pytest.raises(ValueError, match="projection"):
        decode_hdf5(make_h5(projdef=PROJDEF.replace(b"lat_ts=45", b"lat_ts=60")))
    with pytest.raises(ValueError, match="grandeur"):
        decode_hdf5(make_h5(quantity=b"DBZH"))
    with pytest.raises(ValueError, match="HDF5"):
        decode_hdf5(b"pas un fichier")


def test_pooling_keeps_the_strongest_class_of_each_block() -> None:
    classes = np.zeros((SIZE, SIZE), dtype=np.uint8)
    classes[3, 5] = 7
    frame = RadarFrame(end=END, classes=classes)
    assert frame.pooled(4)[0, 1] == 7 and frame.pooled(4).shape == (SIZE // 4, SIZE // 4)
    assert frame.pooled(1) is classes
    with pytest.raises(ValueError):
        frame.pooled(3)


def test_pool_factor_follows_the_zoom() -> None:
    assert pool_factor(3) == 8 and pool_factor(10) == 1
    factors = [pool_factor(z) for z in range(3, 11)]
    assert factors == sorted(factors, reverse=True)  # plus on zoome, moins on réduit


# ------------------------------------------------------------------------------------------------ tuiles


@pytest.mark.parametrize("zoom", [6, 8, 9])
def test_tile_puts_the_rain_where_it_is(zoom: int) -> None:
    frame = frame_with_rain_at(*PARIS, cls=5, half=12)
    x, y, px, py = place_in_tile(*PARIS, zoom)
    image = read_png(tile_png(frame, zoom, x, y))
    assert image.shape == (TILE, TILE)
    assert image[py, px] == 5


def test_tile_rain_does_not_leak_to_a_distant_pixel() -> None:
    frame = frame_with_rain_at(*PARIS, cls=5, half=6)  # 6,5 km de côté
    x, y, _, _ = place_in_tile(*PARIS, 9)
    image = read_png(tile_png(frame, 9, x, y))
    assert 0 < (image == 5).sum() < TILE * TILE // 4
    assert image[0, 0] == 0 or image[TILE - 1, TILE - 1] == 0


def test_tile_is_transparent_outside_the_grid_or_without_rain() -> None:
    frame = frame_with_rain_at(*PARIS)
    assert tile_png(frame, 8, 0, 0) == TRANSPARENT_TILE  # Pacifique, loin de la grille
    dry = RadarFrame(end=END, classes=np.zeros((SIZE, SIZE), dtype=np.uint8))
    assert tile_png(dry, 8, *place_in_tile(*PARIS, 8)[:2]) == TRANSPARENT_TILE
    with pytest.raises(ValueError):
        tile_png(frame, 8, 256, 0)
    with pytest.raises(ValueError):
        tile_png(frame, 11, 0, 0)


def test_png_is_small_and_valid() -> None:
    frame = frame_with_rain_at(*PARIS, half=40)
    png = tile_png(frame, 8, *place_in_tile(*PARIS, 8)[:2])
    assert png != TRANSPARENT_TILE and len(png) < 20_000


# ------------------------------------------------------------------------------------------------ images gardées


def test_store_saves_loads_lists_and_purges(tmp_path: Path) -> None:
    store = RadarStore(tmp_path / "radar", keep_seconds=3600, memory_frames=1)
    assert store.epochs() == [] and store.load(EPOCH) is None
    old = RadarFrame(end=datetime.fromtimestamp(EPOCH - 7200, tz=UTC), classes=np.zeros((SIZE, SIZE), dtype=np.uint8))
    new = frame_with_rain_at(*PARIS)
    store.save(old)
    store.save(new)
    assert store.epochs() == [EPOCH - 7200, EPOCH]
    loaded = store.load(EPOCH)
    assert loaded is not None and np.array_equal(loaded.classes, new.classes)
    assert store.load(EPOCH - 7200) is not None
    assert list(store._memory) == [EPOCH - 7200]  # une seule image en mémoire
    assert store.purge(now=EPOCH + 60) == 1
    assert store.epochs() == [EPOCH]
    (tmp_path / "radar" / "123.rz").write_bytes(b"corrompu")
    assert store.load(123) is None


# ------------------------------------------------------------------------------------------------ téléchargement


def doc(valid: str) -> dict:
    return {"links": [{"rel": "self"}, {"validity_time": valid, "href": "x"}]}


def test_parse_validity() -> None:
    assert parse_validity(doc("2026-11-18T08:25:00Z")) == EPOCH
    for bad in ({}, {"links": []}, [], {"links": [{"rel": "x"}]}):
        with pytest.raises(ValueError):
            parse_validity(bad)


def make_collector(tmp_path: Path, handler, key: str | None = "secret-mf-token-123", enabled: bool = True):
    settings = Settings(METEO_DB_PATH=str(tmp_path / "c.db"), METEOFRANCE_API_KEY=key, METEO_RADAR_ENABLED=enabled)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RadarCollector(client, RadarStore(tmp_path / "radar", 3600), settings)


async def test_refresh_downloads_a_new_image_once_and_only_when_due(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["apikey"] == "secret-mf-token-123"
        if request.url.path.endswith("/produit"):
            assert request.url.params["maille"] == "500"
            return httpx.Response(200, content=make_h5())
        return httpx.Response(200, json=doc("2026-11-18T08:25:00Z"))

    collector = make_collector(tmp_path, handler)
    assert await collector.refresh(now=EPOCH + 150) is True
    assert collector.store.epochs() == [EPOCH] and collector.last_success_at == EPOCH + 150
    assert len(calls) == 2
    # le pas suivant n'est pas encore publié : ni le document ni le fichier ne sont redemandés avant l'heure
    assert await collector.refresh(now=EPOCH + 200) is False
    assert len(calls) == 2
    # l'heure est venue, mais la source n'a pas de nouvelle image : le document seulement, puis on attend 45 s
    assert await collector.refresh(now=EPOCH + FIRST_TRY_AFTER_S + 1) is False
    assert len(calls) == 3 and calls[-1].endswith("LAME_D_EAU")
    assert await collector.refresh(now=EPOCH + FIRST_TRY_AFTER_S + 20) is False
    assert len(calls) == 3


async def test_refresh_survives_a_failing_source_and_hides_the_key(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("échec avec secret-mf-token-123")

    collector = make_collector(tmp_path, handler)
    assert await collector.refresh(now=EPOCH) is False
    assert collector.last_error and "secret-mf-token-123" not in collector.last_error
    assert collector.store.epochs() == []


async def test_refresh_does_nothing_without_a_key_or_when_switched_off(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("aucun appel attendu")

    assert await make_collector(tmp_path, handler, key=None).refresh(now=EPOCH) is False
    assert await make_collector(tmp_path, handler, enabled=False).refresh(now=EPOCH) is False


# ------------------------------------------------------------------------------------------------ routes


def api_client(tmp_path: Path) -> TestClient:
    settings = Settings(METEO_DB_PATH=str(tmp_path / "api.db"), METEOFRANCE_API_KEY="secret-mf-token-123")
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return TestClient(create_app(settings=settings, http_client=client))


def test_routes_without_an_image(tmp_path: Path) -> None:
    with api_client(tmp_path) as client:
        check_without_image(client)


def check_without_image(client: TestClient) -> None:
    assert client.get("/v1/radar/frames").status_code == 503
    assert client.get(f"/v1/radar/tiles/{EPOCH}/8/128/90.png").status_code == 404
    radar = client.get("/v1/health").json()["sources"]["meteofrance-radar"]
    assert radar["status"] == "unavailable" and radar["updated_at"] is None


def test_routes_serve_frames_and_tiles(tmp_path: Path) -> None:
    with api_client(tmp_path) as client:
        check_with_image(client)


def check_with_image(client: TestClient) -> None:
    client.app.state.radar.store.save(frame_with_rain_at(*PARIS, half=12))
    body = client.get("/v1/radar/frames").json()
    assert [f["epoch"] for f in body["frames"]] == [EPOCH]
    assert body["frames"][0]["time"].startswith("2026-11-18T08:25:00")
    assert body["source_model"] == "meteofrance-lame-d-eau" and "Météo-France" in body["attribution"]
    assert body["bounds"][0] < -9 and body["bounds"][3] > 53 and (body["min_zoom"], body["max_zoom"]) == (3, 10)
    assert [c["min_mm_h"] for c in body["legend"]] == [0.1, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    assert body["legend"][0]["color"] == "#a6cee3" and body["legend"][-1]["alpha"] == 1.0
    x, y, px, py = place_in_tile(*PARIS, 8)
    r = client.get(f"/v1/radar/tiles/{EPOCH}/8/{x}/{y}.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "immutable" in r.headers["cache-control"]
    assert read_png(r.content)[py, px] == 5
    assert client.get(f"/v1/radar/tiles/{EPOCH + 300}/8/{x}/{y}.png").status_code == 404
    assert client.get(f"/v1/radar/tiles/{EPOCH}/8/256/0.png").status_code == 404
    assert client.get(f"/v1/radar/tiles/{EPOCH}/11/0/0.png").status_code == 404
    health = client.get("/v1/health").json()["sources"]["meteofrance-radar"]
    assert health["provider"] == "meteofrance-radar"


# ------------------------------------------------------------------------------------------------ zones d'outre-mer
# Fichiers `LAME_D_EAU` des mosaïques ANTILLES, REUNION et NOUVELLE-CALEDONIE lus le 21/11/2026 (Mercator, grilles rectangulaires).

OVERSEAS = [
    # slug, coins (lat, lon) lus dans `where` du vrai fichier : UL, UR, LL, LR ; un point dans la zone vue par les radars
    (
        "antilles",
        {
            "UL": (21.0, -66.0),
            "UR": (20.99999999999914, -57.006187389821896),
            "LL": (10.594135757192138, -65.99999999999999),
            "LR": (10.594135757192138, -57.006187389821896),
        },
        (14.65, -61.0),  # Martinique
    ),
    (
        "reunion",
        {
            "UL": (-16.0, 51.0),
            "UR": (-15.99999999999907, 62.01648998117064),
            "LL": (-25.013510674875672, 50.99999999999999),
            "LR": (-25.013510674875672, 62.01648998117064),
        },
        (-21.1, 55.5),  # La Réunion
    ),
    (
        "new-caledonia",
        {
            "UL": (-16.2, 159.7),
            "UR": (-16.19999999999907, 171.57012385556823),
            "LL": (-27.267467681253866, 159.7),
            "LR": (-27.267467681253866, 171.57012385556823),
        },
        (-22.27, 166.45),  # Nouméa
    ),
]


@pytest.mark.parametrize(("slug", "corners", "place"), OVERSEAS)
def test_overseas_projection_reproduces_the_four_corners_of_the_real_file(slug: str, corners: dict, place: tuple) -> None:
    area = AREAS[slug]
    expected = {"UL": (0.0, area.top_m), "UR": (area.cols * PIXEL_M, area.top_m), "LL": (0.0, 0.0), "LR": (area.cols * PIXEL_M, 0.0)}
    for corner, (lat, lon) in corners.items():
        px, py = area.project(lat, lon)
        assert float(px) == pytest.approx(expected[corner][0], abs=0.05)
        assert float(py) == pytest.approx(expected[corner][1], abs=0.05)
    west, south, east, north = area.bounds  # the box served is the box of the file, widened by at most 0.02 degree
    assert west == pytest.approx(corners["UL"][1], abs=0.02) and east == pytest.approx(corners["UR"][1], abs=0.02)
    assert north == pytest.approx(corners["UL"][0], abs=0.02) and south == pytest.approx(corners["LL"][0], abs=0.02)


@pytest.mark.parametrize(("slug", "corners", "place"), OVERSEAS)
def test_overseas_decode_refuses_another_zones_file_and_reads_its_own(slug: str, corners: dict, place: tuple) -> None:
    area = AREAS[slug]
    data = np.zeros((area.rows, area.cols), dtype=np.uint16)
    data[10, 20] = 1000
    frame = decode_hdf5(make_h5(data, projdef=PROJDEFS[slug], area=area), area)
    assert frame.area is area and frame.classes.shape == (area.rows, area.cols) and frame.classes[10, 20] == 9
    other = AREAS["reunion" if slug != "reunion" else "antilles"]
    with pytest.raises(ValueError, match="projection"):
        decode_hdf5(make_h5(projdef=PROJDEFS[slug], area=area), other)
    with pytest.raises(ValueError, match="projection"):
        decode_hdf5(make_h5(area=area), area)  # the metropolitan projdef is not the one of the area


@pytest.mark.parametrize(("slug", "corners", "place"), OVERSEAS)
@pytest.mark.parametrize("zoom", [5, 7, 9])
def test_overseas_tile_puts_the_rain_where_it_is(slug: str, corners: dict, place: tuple, zoom: int) -> None:
    area = AREAS[slug]
    classes = np.zeros((area.rows, area.cols), dtype=np.uint8)
    x, y = area.project(*place)
    col, row = int(x // PIXEL_M), int((area.top_m - y) // PIXEL_M)
    classes[row - 6 : row + 7, col - 6 : col + 7] = 5
    frame = RadarFrame(end=END, classes=classes, area=area)
    tx, ty, px, py = place_in_tile(*place, zoom)
    image = read_png(tile_png(frame, zoom, tx, ty))
    assert image[py, px] == 5
    assert image.max() == 5 and (image == 5).sum() < TILE * TILE // 2  # a patch of 7 km, not the whole tile
    # a tile on the other side of the Earth does not touch the grid
    assert tile_png(frame, 7, (tx * 2 ** (7 - zoom) + 64) % 128, 60) == TRANSPARENT_TILE


def test_a_grid_that_is_not_a_multiple_of_the_pooling_factor_keeps_its_edge() -> None:
    area = AREAS["antilles"]  # 2310 x 1930: neither is a multiple of 8
    classes = np.zeros((area.rows, area.cols), dtype=np.uint8)
    classes[-1, -1] = 7
    classes[0, 0] = 3
    frame = RadarFrame(end=END, classes=classes, area=area)
    for factor in (2, 4, 8):
        pooled = frame.pooled(factor)
        assert pooled.shape == (-(-area.rows // factor), -(-area.cols // factor))
        assert pooled[-1, -1] == 7 and pooled[0, 0] == 3


def test_store_of_an_overseas_area_keeps_its_shape(tmp_path: Path) -> None:
    area = AREAS["reunion"]
    store = RadarStore(tmp_path / "reunion", keep_seconds=3600, area=area)
    classes = np.zeros((area.rows, area.cols), dtype=np.uint8)
    classes[5, 6] = 4
    store.save(RadarFrame(end=END, classes=classes, area=area))
    loaded = store.load(EPOCH)
    assert loaded is not None and loaded.area is area and loaded.classes[5, 6] == 4
    # an image of another grid (the metropolitan one) is not an image of this area
    other = RadarStore(tmp_path / "reunion", keep_seconds=3600)
    assert other.load(EPOCH) is None


async def test_the_collector_of_an_overseas_area_asks_its_own_zone(tmp_path: Path) -> None:
    area = AREAS["antilles"]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/produit"):
            return httpx.Response(200, content=make_h5(projdef=PROJDEFS["antilles"], area=area))
        return httpx.Response(200, json=doc("2026-11-18T08:25:00Z"))

    settings = Settings(METEO_DB_PATH=str(tmp_path / "c.db"), METEOFRANCE_API_KEY="secret-mf-token-123")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = RadarCollector(client, RadarStore(tmp_path / "antilles", 3600, area=area), settings, area)
    assert await collector.refresh(now=EPOCH + 150) is True
    assert calls == [
        "/public/DPRadar/v1/mosaiques/ANTILLES/observations/LAME_D_EAU",
        "/public/DPRadar/v1/mosaiques/ANTILLES/observations/LAME_D_EAU/produit",
    ]
    assert collector.store.epochs() == [EPOCH]


def test_routes_serve_the_overseas_areas_and_keep_the_old_ones(tmp_path: Path) -> None:
    with api_client(tmp_path) as client:
        assert client.get("/v1/radar/atlantis/frames").status_code == 404
        assert client.get("/v1/radar/antilles/frames").status_code == 503  # nothing downloaded yet
        area = AREAS["antilles"]
        classes = np.zeros((area.rows, area.cols), dtype=np.uint8)
        x, y = area.project(14.65, -61.0)
        col, row = int(x // PIXEL_M), int((area.top_m - y) // PIXEL_M)
        classes[row - 8 : row + 9, col - 8 : col + 9] = 6
        client.app.state.radars["antilles"].store.save(RadarFrame(end=END, classes=classes, area=area))
        body = client.get("/v1/radar/antilles/frames").json()
        assert body["area"] == "antilles" and [f["epoch"] for f in body["frames"]] == [EPOCH]
        assert body["tile_path"] == "/v1/radar/antilles/tiles/{epoch}/{z}/{x}/{y}.png"
        assert body["bounds"][0] == -66.0 and body["bounds"][3] == 21.0
        tx, ty, px, py = place_in_tile(14.65, -61.0, 8)
        tile = client.get(f"/v1/radar/antilles/tiles/{EPOCH}/8/{tx}/{ty}.png")
        assert tile.status_code == 200 and read_png(tile.content)[py, px] == 6
        # the metropolitan routes are untouched, and do not serve the Antilles' image
        assert client.get("/v1/radar/frames").status_code == 503
        assert client.get(f"/v1/radar/tiles/{EPOCH}/8/{tx}/{ty}.png").status_code == 404
        assert client.get("/v1/radar/france/frames").status_code == 503
        sources = client.get("/v1/health").json()["sources"]
        assert sources["meteofrance-radar-antilles"]["status"] in ("fresh", "stale")
        for name in ("meteofrance-radar", "meteofrance-radar-reunion", "meteofrance-radar-new-caledonia"):
            assert sources[name]["status"] == "unavailable"
