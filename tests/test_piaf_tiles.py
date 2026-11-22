"""Tuiles PNG Web Mercator de la pluie dans l'heure PIAF (+5 à +60 min) et routes FastAPI associées.

Aucun appel réseau : la grille PIAF est construite en mémoire avec la géométrie réelle relevée le 17/11/2026
(1651 x 1051 nœuds, pas de 0,01°, 41° N à 51,5° N, -6° E à 10,5° E).
"""

from __future__ import annotations

import math
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.collectors.grib2 import LatLonGrid
from app.collectors.meteofrance import PIAF_STEP_SECONDS, PiafRun, piaf_coverage_id, piaf_run_candidates
from app.config import Settings
from app.main import create_app
from app.piaf_grid import classify_mm_5min, grid_bounds, piaf_tile_png
from app.radar_grid import PALETTE, TILE, TRANSPARENT_TILE, legend
from tests.piaf_grib import PIAF_NI, PIAF_NJ, piaf_filled

RUN_TIME = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
PARIS = (48.8566, 2.3522)


def read_png(png: bytes) -> np.ndarray:
    """Décode les indices de palette d'un PNG 8 bits indexé + tRNS et vérifie ses sommes CRC."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", None
    plte = b""
    trns = b""
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, body = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        expected_crc = struct.unpack(">I", png[pos + 8 + length : pos + 12 + length])[0]
        assert zlib.crc32(kind + body) & 0xFFFFFFFF == expected_crc
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
            assert body[8:10] == bytes([8, 3])
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    assert size is not None
    assert len(plte) == len(PALETTE) * 3
    assert len(trns) == len(PALETTE) and trns[0] == 0
    width, height = size
    rows = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(height, width + 1)
    assert not rows[:, 0].any()
    return rows[:, 1:]


def place_in_tile(lat: float, lon: float, z: int) -> tuple[int, int, int, int]:
    """Coordonnées (tx, ty, px, py) d'un point (lat, lon) dans la pyramide Web Mercator."""
    n = 2**z
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return int(fx), int(fy), int((fx % 1) * TILE), int((fy % 1) * TILE)


def make_piaf_grid(rain_at: tuple[float, float] | None = None, mm_5min: float = 0.4, half: int = 3) -> LatLonGrid:
    """Grille PIAF France métropolitaine (lon0=354.0 comme le GRIB2 réel), sèche ou avec une tache de pluie."""
    values = np.zeros((PIAF_NJ, PIAF_NI), dtype=np.float32)
    grid = LatLonGrid(values=values, lat0=41.0, lon0=354.0, dlat=0.01, dlon=0.01)
    if rain_at is not None:
        idx = grid.index(*rain_at)
        assert idx is not None
        j, i = idx
        values[max(0, j - half) : min(PIAF_NJ, j + half + 1), max(0, i - half) : min(PIAF_NI, i + half + 1)] = mm_5min
    return grid


# ------------------------------------------------------------------------------------------------ géométrie & classes


def test_grid_bounds_normalizes_grib2_360_longitude() -> None:
    grib_grid = make_piaf_grid()
    assert grid_bounds(grib_grid) == (-6.0, 41.0, 10.5, 51.5)
    signed_grid = LatLonGrid(values=grib_grid.values, lat0=41.0, lon0=-6.0, dlat=0.01, dlon=0.01)
    assert grid_bounds(signed_grid) == (-6.0, 41.0, 10.5, 51.5)


def test_classify_mm_5min_matches_radar_thresholds_and_discards_invalid() -> None:
    # 1 mm en 5 min = 12 mm/h
    samples = np.array(
        [[0.0, 0.005, 0.01, 0.05, 0.1, 0.2, 0.4, 1.0, 2.0, 3.0, 6.0, -1.0, 600.0, np.nan, np.inf]],
        dtype=np.float32,
    )
    assert classify_mm_5min(samples).tolist() == [[0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 0, 0, 0]]


# ------------------------------------------------------------------------------------------------ rendu tuiles


@pytest.mark.parametrize("zoom", [3, 5, 6, 8, 9, 10])
def test_piaf_tile_places_rain_accurately_across_zooms(zoom: int) -> None:
    # 0,4 mm en 5 min = 4,8 mm/h -> classe 5 (>= 4,0 et < 8,0 mm/h)
    grid = make_piaf_grid(rain_at=PARIS, mm_5min=0.4, half=4)
    tx, ty, px, py = place_in_tile(*PARIS, zoom)
    png = piaf_tile_png(grid, zoom, tx, ty)
    assert png != TRANSPARENT_TILE and len(png) < 20_000
    image = read_png(png)
    assert image.shape == (TILE, TILE)
    assert image[py, px] == 5


def test_piaf_tile_keeps_small_shower_visible_when_zoomed_out() -> None:
    # Une petite cellule orageuse de 2 x 2 km (classe 7 : 2,0 mm en 5 min = 24 mm/h) reste visible au zoom 4 et 5.
    grid = make_piaf_grid(rain_at=PARIS, mm_5min=2.0, half=1)
    for zoom in (4, 5):
        tx, ty, px, py = place_in_tile(*PARIS, zoom)
        image = read_png(piaf_tile_png(grid, zoom, tx, ty))
        assert image[py, px] == 7


def test_piaf_tile_rain_does_not_leak_to_distant_pixels() -> None:
    grid = make_piaf_grid(rain_at=PARIS, mm_5min=0.4, half=3)
    tx, ty, _, _ = place_in_tile(*PARIS, 9)
    image = read_png(piaf_tile_png(grid, 9, tx, ty))
    assert 0 < (image == 5).sum() < TILE * TILE // 4
    assert image[0, 0] == 0 or image[TILE - 1, TILE - 1] == 0


def test_piaf_tile_is_transparent_when_dry_or_outside_bounds() -> None:
    wet = make_piaf_grid(rain_at=PARIS, mm_5min=1.0, half=5)
    # Pacifique, Amérique du Nord et sud de l'Espagne (hors boîte [-6, 41, 10.5, 51.5])
    assert piaf_tile_png(wet, 8, 0, 0) == TRANSPARENT_TILE
    ny_x, ny_y, _, _ = place_in_tile(40.71, -74.0, 8)
    assert piaf_tile_png(wet, 8, ny_x, ny_y) == TRANSPARENT_TILE
    malaga_x, malaga_y, _, _ = place_in_tile(36.7, -4.4, 8)
    assert piaf_tile_png(wet, 8, malaga_x, malaga_y) == TRANSPARENT_TILE

    dry = make_piaf_grid(rain_at=None)
    tx, ty, _, _ = place_in_tile(*PARIS, 8)
    assert piaf_tile_png(dry, 8, tx, ty) == TRANSPARENT_TILE


def test_piaf_tile_rejects_out_of_range_zoom_and_coords() -> None:
    grid = make_piaf_grid()
    for z, x, y in ((2, 0, 0), (11, 0, 0), (8, -1, 0), (8, 256, 0), (8, 0, -1), (8, 0, 256)):
        with pytest.raises(ValueError):
            piaf_tile_png(grid, z, x, y)


# ------------------------------------------------------------------------------------------------ routes FastAPI


def api_client(
    tmp_path: Path,
    calls: list[httpx.Request],
    *,
    handler=None,
    api_key: str | None = "secret-mf-token-123",
    **extra_settings: object,
) -> TestClient:
    def default_fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    def dispatch(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    settings = Settings(
        METEO_DB_PATH=str(tmp_path / "api.db"),
        METEOFRANCE_API_KEY=api_key,
        **extra_settings,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch if handler is not None else default_fail))
    return TestClient(create_app(settings=settings, http_client=client))


def make_wcs_handler(
    published: set[str] | None = None,
    *,
    rain_step_seconds: int = 600,
    rain_mm_5min: float = 0.4,
):
    """Gestionnaire WCS PIAF en mémoire : sec partout sauf `rain_step_seconds` à `rain_mm_5min`."""

    def handle(request: httpx.Request) -> httpx.Response:
        coverage = request.url.params["coverageid"]
        if published is not None and coverage not in published:
            return httpx.Response(404, text="<mw:fault/>")
        step = int(next(s for s in request.url.params.get_list("subset") if s.startswith("time("))[5:-1])
        value = rain_mm_5min if step == rain_step_seconds else 0.0
        return httpx.Response(200, content=piaf_filled(value))

    return handle


def test_rain_nowcast_routes_return_503_when_upstream_fails(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    with api_client(tmp_path, calls) as client:
        r_frames = client.get("/v1/rain-nowcast/frames")
        assert r_frames.status_code == 503
        assert r_frames.headers["content-type"].startswith("application/problem+json")

        epoch = int((RUN_TIME + timedelta(minutes=5)).timestamp())
        tx, ty, _, _ = place_in_tile(*PARIS, 8)
        r_tile = client.get(f"/v1/rain-nowcast/tiles/{epoch}/8/{tx}/{ty}.png")
        assert r_tile.status_code == 503
        assert r_tile.headers["content-type"].startswith("application/problem+json")

    assert len(calls) >= 2  # chargement à la demande tenté et mis en échec proprement


def test_rain_nowcast_routes_return_503_without_api_key_or_when_circuit_open(tmp_path: Path) -> None:
    # Sans clé API : 503 immédiat, 0 requête réseau.
    calls_no_key: list[httpx.Request] = []
    tx, ty, _, _ = place_in_tile(*PARIS, 8)
    epoch = int((RUN_TIME + timedelta(minutes=5)).timestamp())
    with api_client(tmp_path / "nokey", calls_no_key, api_key=None) as client:
        assert client.get("/v1/rain-nowcast/frames").status_code == 503
        assert client.get(f"/v1/rain-nowcast/tiles/{epoch}/8/{tx}/{ty}.png").status_code == 503
    assert calls_no_key == []

    # Circuit breaker ouvert : 503 immédiat, 0 requête réseau.
    calls_open: list[httpx.Request] = []
    with api_client(tmp_path / "open", calls_open, METEO_CIRCUIT_FAIL_THRESHOLD=1) as client:
        client.app.state.cache.record_failure("meteofrance-nowcast", "502", fail_threshold=1, cooldown_seconds=300)
        assert client.get("/v1/rain-nowcast/frames").status_code == 503
        assert client.get(f"/v1/rain-nowcast/tiles/{epoch}/8/{tx}/{ty}.png").status_code == 503
    assert calls_open == []


def test_rain_nowcast_frames_loads_piaf_on_cold_start(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    latest_run = piaf_run_candidates(datetime.now(UTC), publish_delay_minutes=10)[0]
    wcs = make_wcs_handler(published={piaf_coverage_id(latest_run)}, rain_step_seconds=600, rain_mm_5min=0.4)

    with api_client(tmp_path, calls, handler=wcs, METEO_PIAF_STEPS=4) as client:
        assert client.app.state.now_collector.current_run() is None

        r_frames = client.get("/v1/rain-nowcast/frames")
        assert r_frames.status_code == 200
        body = r_frames.json()
        assert body["source_model"] == "meteofrance-piaf"
        assert body["run_utc"] == latest_run.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert len(body["frames"]) == 4
        assert len(calls) == 4  # 4 échéances chargées une fois

        # Appels suivants servis depuis la mémoire sans requête réseau.
        assert client.get("/v1/rain-nowcast/frames").status_code == 200
        wet_epoch = body["frames"][1]["epoch"]
        tx, ty, px, py = place_in_tile(*PARIS, 8)
        r_tile = client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/8/{tx}/{ty}.png")
        assert r_tile.status_code == 200
        assert read_png(r_tile.content)[py, px] == 5
        assert len(calls) == 4


def test_rain_nowcast_tile_loads_piaf_on_cold_start(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    latest_run = piaf_run_candidates(datetime.now(UTC), publish_delay_minutes=10)[0]
    wcs = make_wcs_handler(published={piaf_coverage_id(latest_run)}, rain_step_seconds=600, rain_mm_5min=0.4)

    with api_client(tmp_path, calls, handler=wcs, METEO_PIAF_STEPS=4) as client:
        assert client.app.state.now_collector.current_run() is None
        wet_epoch = int((latest_run + timedelta(minutes=10)).timestamp())
        tx, ty, px, py = place_in_tile(*PARIS, 8)

        r_tile = client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/8/{tx}/{ty}.png")
        assert r_tile.status_code == 200
        assert r_tile.headers["content-type"] == "image/png"
        assert read_png(r_tile.content)[py, px] == 5
        assert len(calls) == 4


def test_rain_nowcast_tile_refreshes_when_newer_epoch_requested(tmp_path: Path) -> None:
    """Simule un 2e worker uvicorn qui détient encore le run précédent quand le 1er a publié un run plus récent."""
    calls: list[httpx.Request] = []
    latest_run, older_run = piaf_run_candidates(datetime.now(UTC), publish_delay_minutes=10)[:2]
    # La nouvelle échéance (+20 min du nouveau run = step 4, soit 1200 s) contient de la pluie sur Paris.
    wcs = make_wcs_handler(published={piaf_coverage_id(latest_run)}, rain_step_seconds=1200, rain_mm_5min=0.4)

    with api_client(tmp_path, calls, handler=wcs, METEO_PIAF_STEPS=4) as client:
        # Le worker 2 vient tout juste de charger `older_run` (age < TTL : frais, mais en retard d'un run).
        dry = make_piaf_grid(rain_at=None)
        client.app.state.now_collector._run = PiafRun(run=older_run, steps=[dry] * 4)

        newest_epoch = int((latest_run + timedelta(minutes=20)).timestamp())
        assert client.app.state.now_collector._run.step_for_epoch(newest_epoch) is None

        tx, ty, px, py = place_in_tile(*PARIS, 8)
        r_tile = client.get(f"/v1/rain-nowcast/tiles/{newest_epoch}/8/{tx}/{ty}.png")
        assert r_tile.status_code == 200
        assert r_tile.headers["content-type"] == "image/png"
        assert read_png(r_tile.content)[py, px] == 5
        assert client.app.state.now_collector._run.run == latest_run
        assert len(calls) == 4


def test_rain_nowcast_frames_and_tiles_routes(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    dry = make_piaf_grid(rain_at=None)
    wet_paris = make_piaf_grid(rain_at=PARIS, mm_5min=0.4, half=4)  # 4,8 mm/h -> classe 5
    steps = [dry, wet_paris, *[dry for _ in range(10)]]

    with api_client(tmp_path, calls) as client:
        client.app.state.now_collector._run = PiafRun(run=RUN_TIME, steps=steps)

        r_frames = client.get("/v1/rain-nowcast/frames")
        assert r_frames.status_code == 200
        assert r_frames.headers["cache-control"] == "public, max-age=60"
        body = r_frames.json()
        assert body["source_model"] == "meteofrance-piaf"
        assert body["run_utc"] == "2026-11-22T12:00:00Z"
        assert body["bounds"] == [-6.0, 41.0, 10.5, 51.5]
        assert body["min_zoom"] == 3 and body["max_zoom"] == 10
        assert body["step_seconds"] == PIAF_STEP_SECONDS
        assert body["tile_path"] == "/v1/rain-nowcast/tiles/{epoch}/{z}/{x}/{y}.png"
        assert body["legend"] == legend()
        assert "Météo-France" in body["attribution"] and "PIAF" in body["attribution"]
        assert len(body["frames"]) == 12
        assert [f["lead_minutes"] for f in body["frames"]] == [5 * k for k in range(1, 13)]
        assert body["frames"][0]["time_utc"] == "2026-11-22T12:05:00Z"
        assert body["frames"][1]["time_utc"] == "2026-11-22T12:10:00Z"
        assert body["frames"][-1]["time_utc"] == "2026-11-22T13:00:00Z"

        dry_epoch = body["frames"][0]["epoch"]
        wet_epoch = body["frames"][1]["epoch"]
        tx, ty, px, py = place_in_tile(*PARIS, 8)

        # Tuile pluvieuse sur Paris (+10 min)
        r_wet = client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/8/{tx}/{ty}.png")
        assert r_wet.status_code == 200
        assert r_wet.headers["content-type"] == "image/png"
        assert r_wet.headers["cache-control"] == "public, max-age=3600, immutable"
        assert read_png(r_wet.content)[py, px] == 5

        # Tuile sèche sur Paris (+5 min) -> transparente
        r_dry = client.get(f"/v1/rain-nowcast/tiles/{dry_epoch}/8/{tx}/{ty}.png")
        assert r_dry.status_code == 200
        assert r_dry.content == TRANSPARENT_TILE

        # Tuile hors de France -> transparente
        r_out = client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/8/0/0.png")
        assert r_out.status_code == 200
        assert r_out.content == TRANSPARENT_TILE

        # 404 sur epoch absent du run courant (passé, non aligné, ou trop loin dans le futur) -> 0 appel réseau
        assert client.get(f"/v1/rain-nowcast/tiles/{int(RUN_TIME.timestamp())}/8/{tx}/{ty}.png").status_code == 404
        assert client.get(f"/v1/rain-nowcast/tiles/{wet_epoch + 1}/8/{tx}/{ty}.png").status_code == 404
        far_future = int(datetime.now(UTC).timestamp()) + 86400
        far_future -= far_future % PIAF_STEP_SECONDS
        assert client.get(f"/v1/rain-nowcast/tiles/{far_future}/8/{tx}/{ty}.png").status_code == 404

        # 404 sur zoom hors [3, 10] ou coordonnées x/y hors limites -> 0 appel réseau
        assert client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/2/1/1.png").status_code == 404
        assert client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/11/0/0.png").status_code == 404
        assert client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/8/256/0.png").status_code == 404
        assert client.get(f"/v1/rain-nowcast/tiles/{wet_epoch}/8/0/-1.png").status_code == 404

        assert calls == []  # 0 appel réseau tant qu'on ne réclame pas d'échéance plus récente plausible

        # Échéance alignée postérieure au run chargé : tente un rafraîchissement, échoue en amont -> repli 404 propre
        assert client.get(f"/v1/rain-nowcast/tiles/{wet_epoch + 7200}/8/{tx}/{ty}.png").status_code == 404
