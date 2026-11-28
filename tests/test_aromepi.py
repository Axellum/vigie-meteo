"""AROME-PI : réduction des cartes, lecture d'un point, runs gardés sur disque, téléchargement et route `/v1/shortterm`.

Les cartes de test ont la géométrie réelle du WCS (1651 x 1051 nœuds à 0,01°, relevée le 18/11/2026) ; les codes de type de
précipitation sont ceux de la page de Météo-France « Comprendre les diagnostics de type de précipitation dans les modèles AROME ».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.aromepi_grid import POOL, STEPS, AromePiRun, Geometry, pool_code, pool_max, pool_mean, pool_min
from app.collectors.aromepi import (
    PRODUCTS,
    AromePiCollector,
    AromePiStore,
    reduce_step,
    run_candidates,
    run_stamp,
)
from app.collectors.grib2 import LatLonGrid, decode_latlon_simple
from app.config import Settings
from app.main import create_app
from tests.piaf_grib import PIAF_NI, PIAF_NJ, encode

PARIS = (48.85, 2.35)
PARIS_J, PARIS_I = round((PARIS[0] - 41.0) / 0.01), round((PARIS[1] + 6.0) / 0.01)  # 785, 835
SPOT_STEP = 5  # l'échéance où le test met son phénomène à Paris (la 5e = +75 min)


# ------------------------------------------------------------------------------------------------ la réduction


def test_pooling_keeps_the_worst_of_each_block_and_pads_the_edges() -> None:
    values = np.zeros((6, 6), dtype=np.float32)  # 6 n'est pas un multiple de 4 : bords prolongés
    values[1, 1] = 9.0
    values[5, 5] = 7.0
    assert pool_max(values).shape == (2, 2)
    assert pool_max(values).tolist() == [[9.0, 0.0], [0.0, 7.0]]
    visibility = np.full((6, 6), 20000.0, dtype=np.float32)
    visibility[0, 3] = 150.0
    assert pool_min(visibility).tolist() == [[150.0, 20000.0], [20000.0, 20000.0]]


def test_rain_is_the_mean_of_the_block_not_its_worst() -> None:
    rain = np.zeros((4, 8), dtype=np.float32)
    rain[0, 0] = 8.0  # une averse sur un seul nœud du bloc (0, 0)
    rain[:, 4:] = 2.0  # une pluie régulière sur tout le bloc (0, 1)
    assert pool_mean(rain).tolist() == [[0.5, 2.0]]
    by_key = {p.key: p for p in PRODUCTS}
    reduced = reduce_step(by_key["rain"], LatLonGrid(rain, 41.0, 354.0, 0.01, 0.01))
    assert reduced.dtype == np.float16 and reduced.tolist() == [[0.5, 2.0]]
    negative = LatLonGrid(np.full((4, 4), -0.01, dtype=np.float32), 41.0, 354.0, 0.01, 0.01)
    assert reduce_step(by_key["rain"], negative).tolist() == [[0.0]]  # un arrondi négatif du modèle n'est pas une pluie


def test_pooling_a_code_takes_the_centre_but_freezing_and_hail_win() -> None:
    codes = np.zeros((8, 8), dtype=np.float32)
    codes[2, 2] = 1  # le nœud central du bloc (0, 0) : pluie
    codes[0, 0] = 11  # bruine dans un coin : ignorée
    codes[5, 6] = 12  # bruine verglaçante hors du centre du bloc (1, 1)
    codes[5, 1] = 5  # neige hors du centre du bloc (1, 0) : ignorée
    codes[2, 6] = 10  # grêle dans le bloc (0, 1)
    codes[7, 7] = 3  # verglaçante dans le même bloc que la bruine verglaçante : la plus grave l'emporte
    out = pool_code(codes)
    assert out.tolist() == [[1, 10], [0, 3]]


def test_reduce_step_stores_each_product_in_its_own_unit() -> None:
    grid = LatLonGrid(np.zeros((4, 4), dtype=np.float32), 41.0, 354.0, 0.01, 0.01)
    gust = LatLonGrid(np.full((4, 4), 25.0, dtype=np.float32), 41.0, 354.0, 0.01, 0.01)
    by_key = {p.key: p for p in PRODUCTS}
    assert reduce_step(by_key["gust"], gust).tolist() == [[90]]  # 25 m/s = 90 km/h
    huge = LatLonGrid(np.full((4, 4), 90.0, dtype=np.float32), 41.0, 354.0, 0.01, 0.01)
    assert reduce_step(by_key["gust"], huge).tolist() == [[255]]  # au-delà de 255 km/h : borné, jamais enroulé
    assert reduce_step(by_key["visibility"], LatLonGrid(np.full((4, 4), 20000.0, dtype=np.float32), 41.0, 354.0, 0.01, 0.01)).dtype == np.uint16
    assert reduce_step(by_key["hail"], grid).dtype == np.float16
    assert reduce_step(by_key["ptype"], grid).dtype == np.uint8


def test_geometry_finds_the_block_of_a_point_and_knows_its_edges() -> None:
    geometry = Geometry(41.0, 354.0, 0.01, 0.01, PIAF_NJ, PIAF_NI)  # 354° = -6° E, comme dans le GRIB2
    assert geometry.cell(*PARIS) == (PARIS_J // POOL, PARIS_I // POOL)
    assert geometry.cell(41.0, -6.0) == (0, 0)
    assert geometry.cell(51.5, 10.5) == (1050 // POOL, 1650 // POOL)
    assert geometry.cell(64.1, -21.9) is None  # Reykjavik
    assert geometry.cell(40.0, 2.0) is None
    assert geometry.cell(48.85, 20.0) is None


# ------------------------------------------------------------------------------------------------ les runs sur disque


def make_run(run: datetime, gust: int = 30, with_rain: bool = True) -> AromePiRun:
    geometry = Geometry(41.0, 354.0, 0.01, 0.01, PIAF_NJ, PIAF_NI)
    nj, ni = -(-PIAF_NJ // POOL), -(-PIAF_NI // POOL)
    gusts = np.full((STEPS, nj, ni), 12, dtype=np.uint8)
    hail = np.zeros((STEPS, nj, ni), dtype=np.float16)
    visibility = np.full((STEPS, nj, ni), 20000, dtype=np.uint16)
    ptype = np.zeros((STEPS, nj, ni), dtype=np.uint8)
    rain = np.zeros((STEPS, nj, ni), dtype=np.float16)
    j, i = PARIS_J // POOL, PARIS_I // POOL
    gusts[SPOT_STEP, j, i] = gust
    hail[SPOT_STEP, j, i] = 17.5
    visibility[SPOT_STEP, j, i] = 120
    ptype[SPOT_STEP, j, i] = 10
    rain[SPOT_STEP, j, i] = 3.5
    return AromePiRun(
        run=run, geometry=geometry, gust_kmh=gusts, hail=hail, visibility_m=visibility, ptype=ptype, rain_mm=None if not with_rain else rain
    )


def test_store_saves_loads_lists_purges_and_hands_over_the_newest(tmp_path: Path) -> None:
    store = AromePiStore(tmp_path / "aromepi", keep_runs=2)
    assert store.epochs() == [] and store.latest() is None
    base = datetime(2026, 11, 18, 6, tzinfo=UTC)
    for hours in range(3):
        store.save(make_run(base + timedelta(hours=hours)))
    assert len(store.epochs()) == 3
    assert store.purge() == 1 and len(store.epochs()) == 2
    run = store.latest()
    assert run is not None and run.run == base + timedelta(hours=2)
    assert run.point(*PARIS) == {
        "gust_kmh": [12] * SPOT_STEP + [30] + [12] * (STEPS - SPOT_STEP - 1),
        "hail_kg_m2": [0.0] * SPOT_STEP + [17.5] + [0.0] * (STEPS - SPOT_STEP - 1),
        "visibility_m": [20000] * SPOT_STEP + [120] + [20000] * (STEPS - SPOT_STEP - 1),
        "precip_type": [0] * SPOT_STEP + [10] + [0] * (STEPS - SPOT_STEP - 1),
        "rain_mm": [0.0] * SPOT_STEP + [3.5] + [0.0] * (STEPS - SPOT_STEP - 1),
    }
    assert run.point(64.1, -21.9) is None
    assert store.latest() is run  # le même objet tant qu'aucun run plus récent n'est écrit
    store.save(make_run(base + timedelta(hours=3)))  # écrit par l'autre processus
    newest = store.latest()
    assert newest is not None and newest.run == base + timedelta(hours=3)


def test_a_run_kept_before_the_rain_is_still_served_without_it(tmp_path: Path) -> None:
    store = AromePiStore(tmp_path)
    store.save(make_run(datetime(2026, 11, 18, 6, tzinfo=UTC), with_rain=False))  # le fichier d'une passerelle d'avant
    run = store.latest()
    assert run is not None and run.rain_mm is None
    point = run.point(*PARIS)
    assert point is not None and point["rain_mm"] == [None] * STEPS and point["hail_kg_m2"][SPOT_STEP] == 17.5


def test_store_skips_an_unreadable_newest_file(tmp_path: Path) -> None:
    store = AromePiStore(tmp_path)
    base = datetime(2026, 11, 18, 6, tzinfo=UTC)
    store.save(make_run(base))
    (tmp_path / f"{int((base + timedelta(hours=1)).timestamp())}.npz").write_bytes(b"not a zip")
    run = store.latest()
    assert run is not None and run.run == base


def test_candidates_follow_the_publication_delay() -> None:
    at = datetime(2026, 11, 18, 9, 19, tzinfo=UTC)
    assert run_candidates(at, 20) == [datetime(2026, 11, 18, 8, tzinfo=UTC), datetime(2026, 11, 18, 7, tzinfo=UTC)]
    at = datetime(2026, 11, 18, 9, 21, tzinfo=UTC)
    assert run_candidates(at, 20)[0] == datetime(2026, 11, 18, 9, tzinfo=UTC)
    assert run_stamp(datetime(2026, 11, 18, 9, tzinfo=UTC)) == "2026-11-18T09.00.00Z"


# ------------------------------------------------------------------------------------------------ le téléchargement


def _frame(step: int, kind: str) -> bytes:
    """Une carte de la France entière de ce produit à cette échéance (celle de la forme du vrai WCS)."""
    background = {"gust": 8.0, "hail": 0.0, "visibility": 20000.0, "ptype": 0.0, "rain": 0.0}[kind]
    values = np.full((PIAF_NJ, PIAF_NI), background)
    if step == SPOT_STEP + 1:  # les échéances comptent de 1 (15 min) à 24 (6 h)
        spot = (slice(PARIS_J - 1, PARIS_J + 2), slice(PARIS_I - 1, PARIS_I + 2))
        values[spot] = {"gust": 25.0, "hail": 17.5, "visibility": 120.0, "ptype": 10.0, "rain": 4.0}[kind]
    decimal = {"gust": 2, "hail": 2, "visibility": 0, "ptype": 0, "rain": 2}[kind]
    return encode(values[::-1], decimal=decimal)  # `encode` écrit la ligne 0 au nord, `values` compte du sud


class FakeWcs:
    """Le WCS AROME-PI : `published` = heures de run listées, `broken` = (produit, échéance) qui répondent 404."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.published: set[str] | None = None
        self.broken: set[tuple[str, int]] = set()
        self.fail_all = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_all:
            return httpx.Response(502, text="bad gateway secret-mf-token-123")
        params = request.url.params
        coverage = params["coverageid"]
        if self.published is not None and not any(stamp in coverage for stamp in self.published):
            return httpx.Response(404, text="<mw:fault/>")
        if request.url.path.endswith("/DescribeCoverage"):
            return httpx.Response(200, text="<wcs:CoverageDescriptions/>")
        kind = next(p.key for p in PRODUCTS if coverage.startswith(p.coverage.split("___")[0]))
        step = int(next(s for s in params.get_list("subset") if s.startswith("time("))[5:-1]) // 900
        if (kind, step) in self.broken:
            return httpx.Response(404, text="<mw:fault/>")
        return httpx.Response(200, content=_frame(step, kind))


def make_collector(tmp_path: Path, wcs: FakeWcs, key: str | None = "secret-mf-token-123", enabled: bool = True) -> AromePiCollector:
    cfg = Settings(
        METEO_DB_PATH=str(tmp_path / "cache.db"),
        METEOFRANCE_API_KEY=key,
        METEO_AROMEPI_ENABLED=enabled,
        METEO_AROMEPI_CONCURRENCY=4,
    )
    cfg.aromepi_requests_per_minute = 60_000  # le vrai espacement est réglé pour le quota : pas d'attente en test
    client = httpx.AsyncClient(transport=httpx.MockTransport(wcs))
    return AromePiCollector(client, AromePiStore(tmp_path / "aromepi"), cfg)


NOW = datetime(2026, 11, 18, 9, 25, tzinfo=UTC).timestamp()  # le run de 09 h (publié vers 09 h 19) est dû


async def test_a_run_costs_120_maps_and_a_point_reads_them(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is True
    gets = [r for r in wcs.requests if r.url.path.endswith("/GetCoverage")]
    assert len(gets) == 5 * STEPS
    rain_gets = [r for r in gets if r.url.params["coverageid"].startswith("TOTAL_PRECIPITATION__")]
    assert len(rain_gets) == STEPS and all(r.url.params["coverageid"].endswith("Z_PT15M") for r in rain_gets)
    assert len([r for r in wcs.requests if r.url.path.endswith("/DescribeCoverage")]) == 1
    assert {r.url.params["format"] for r in gets} == {"application/wmo-grib"}
    assert {"long(-6,10.5)", "lat(41,51.5)"} <= set(gets[0].url.params.get_list("subset"))
    assert all("height(10)" in r.url.params.get_list("subset") for r in gets if r.url.params["coverageid"].startswith("WIND_SPEED_GUST_15MIN"))
    assert all("secret" not in str(r.url) for r in wcs.requests)  # la clé voyage dans un en-tête, jamais dans l'adresse
    run = collector.store.latest()
    assert run is not None and run.run == datetime(2026, 11, 18, 9, tzinfo=UTC)
    point = run.point(*PARIS)
    assert point is not None
    assert point["gust_kmh"][SPOT_STEP] == 90 and point["gust_kmh"][SPOT_STEP - 1] == 29  # 25 m/s, puis 8 m/s
    assert point["hail_kg_m2"][SPOT_STEP] == 17.5 and point["visibility_m"][SPOT_STEP] == 120 and point["precip_type"][SPOT_STEP] == 10
    # Pluie de 4 mm sur 3 x 3 nœuds autour de Paris : 6 d'entre eux tombent dans le bloc de Paris (lignes 784-786, colonnes 834-835),
    # soit une moyenne de 4 x 6 / 16 = 1,5 mm ; l'échéance d'avant est sèche.
    assert point["rain_mm"][SPOT_STEP] == 1.5 and point["rain_mm"][SPOT_STEP - 1] == 0.0
    far = run.point(43.3, 5.4)  # Marseille : le fond
    assert far is not None and far["visibility_m"] == [20000] * STEPS and far["rain_mm"] == [0.0] * STEPS


async def test_nothing_is_asked_when_the_run_is_already_kept_or_not_due(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is True
    asked = len(wcs.requests)
    assert await collector.refresh(NOW + 60) is False  # trop tôt pour réessayer
    assert await collector.refresh(NOW + 600) is False  # le run de 09 h est déjà gardé, celui de 10 h n'est pas dû
    assert len(wcs.requests) == asked


async def test_an_unpublished_run_is_probed_not_downloaded_and_the_previous_one_is_not_taken_again(tmp_path: Path) -> None:
    wcs = FakeWcs()
    wcs.published = set()  # rien n'est encore publié
    collector = make_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is False
    assert len(wcs.requests) == 2  # une sonde par run candidat (09 h et 08 h), aucune carte
    assert all(r.url.path.endswith("/DescribeCoverage") for r in wcs.requests)
    wcs.published = {"2026-11-18T08.00.00Z"}  # 09 h pas encore là : on prend 08 h
    assert await collector.refresh(NOW + 200) is True
    run = collector.store.latest()
    assert run is not None and run.run == datetime(2026, 11, 18, 8, tzinfo=UTC)


async def test_an_incomplete_run_is_never_served_and_the_retry_skips_what_was_read(tmp_path: Path) -> None:
    wcs = FakeWcs()
    wcs.broken = {("visibility", 7)}
    collector = make_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is False
    assert collector.store.latest() is None
    assert collector.last_error is not None and "pas encore publiée" in collector.last_error
    first_pass = len(wcs.requests)
    wcs.broken = set()
    assert await collector.refresh(NOW + 200) is True
    second_pass = len(wcs.requests) - first_pass
    # Les échéances déjà lues ne sont pas redemandées : la 1re passe a lu les rafales, la grêle et 6 visibilités (~55 cartes),
    # la reprise lit le reste (~70 avec les sondes), loin des 120 d'un run entier.
    assert second_pass < 5 * STEPS // 2 + 20
    run = collector.store.latest()
    assert run is not None and run.point(*PARIS)["visibility_m"][SPOT_STEP] == 120  # type: ignore[index]


async def test_a_failing_source_leaves_the_kept_run_and_hides_the_key(tmp_path: Path) -> None:
    wcs = FakeWcs()
    collector = make_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is True
    wcs.fail_all = True
    assert await collector.refresh(NOW + 3600 + 1200) is False
    assert collector.last_error is not None and "secret-mf-token-123" not in collector.last_error
    kept = collector.store.latest()
    assert kept is not None and kept.run == datetime(2026, 11, 18, 9, tzinfo=UTC)


async def test_nothing_happens_without_a_key_or_when_switched_off(tmp_path: Path) -> None:
    for key, enabled in ((None, True), ("  ", True), ("secret-mf-token-123", False)):
        wcs = FakeWcs()
        collector = make_collector(tmp_path / f"{key}-{enabled}".replace(" ", "_").replace("None", "no"), wcs, key=key, enabled=enabled)
        assert collector.enabled() is False
        assert await collector.refresh(NOW) is False
        assert wcs.requests == []


def test_a_wcs_frame_the_decoder_reads_has_the_expected_shape() -> None:
    grid = decode_latlon_simple(_frame(SPOT_STEP + 1, "gust"))
    assert grid.values.shape == (PIAF_NJ, PIAF_NI)


# ------------------------------------------------------------------------------------------------ la route


def api_client(tmp_path: Path, run: AromePiRun | None, max_age: int = 180) -> TestClient:
    cfg = Settings(
        METEO_DB_PATH=str(tmp_path / "cache.db"),
        METEOFRANCE_API_KEY="secret-mf-token-123",
        METEO_AROMEPI_MAX_AGE_MINUTES=max_age,
    )
    if run is not None:
        AromePiStore(tmp_path / "aromepi").save(run)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    return TestClient(create_app(settings=cfg, http_client=client))


def recent_run(minutes_ago: int = 30) -> datetime:
    """Le run de l'heure pleine la plus proche d'il y a `minutes_ago` minutes, comme le serait un run publié."""
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(minute=0, second=0, microsecond=0)


def test_route_serves_the_steps_not_yet_passed(tmp_path: Path) -> None:
    run_time = recent_run()
    with api_client(tmp_path, make_run(run_time)) as client:
        resp = client.get("/v1/shortterm", params={"lat": PARIS[0], "lon": PARIS[1]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["source_model"] == "meteofrance-arome-pi" and "Météo-France" in body["attribution"]
        assert body["interval_minutes"] == 15 and body["run"].startswith(run_time.strftime("%Y-%m-%dT%H:00:00"))
        times = [datetime.fromisoformat(s["time"].replace("Z", "+00:00")) for s in body["steps"]]
        assert times == sorted(times) and times[0] >= datetime.now(UTC) - timedelta(seconds=1)
        assert times[-1] == run_time + timedelta(hours=6)
        by_time = {t: s for t, s in zip(times, body["steps"], strict=True)}
        spot = by_time.get(run_time + timedelta(minutes=15 * (SPOT_STEP + 1)))
        if spot is not None:  # l'échéance du phénomène n'est pas encore passée selon l'heure du test
            assert spot == {
                "time": spot["time"],
                "wind_gust_kmh": 30,
                "hail_kg_m2": 17.5,
                "visibility_m": 120,
                "precip_type": 10,
                "rain_mm": 3.5,
            }
        assert resp.headers["cache-control"] == "public, max-age=300"


def test_route_outside_the_map_and_without_a_run(tmp_path: Path) -> None:
    with api_client(tmp_path / "a", make_run(recent_run())) as client:
        assert client.get("/v1/shortterm", params={"lat": 64.1, "lon": -21.9}).status_code == 404
        assert client.get("/v1/shortterm", params={"lat": 95, "lon": 2}).status_code == 422
    with api_client(tmp_path / "b", None) as client:
        resp = client.get("/v1/shortterm", params={"lat": PARIS[0], "lon": PARIS[1]})
        assert resp.status_code == 503
    with api_client(tmp_path / "c", make_run(recent_run(minutes_ago=300)), max_age=120) as client:
        assert client.get("/v1/shortterm", params={"lat": PARIS[0], "lon": PARIS[1]}).status_code == 503  # run trop vieux


def test_health_says_whether_the_run_is_fresh(tmp_path: Path) -> None:
    with api_client(tmp_path / "a", make_run(recent_run())) as client:
        source = client.get("/v1/health").json()["sources"]["meteofrance-aromepi"]
        assert source["status"] == "fresh" and source["updated_at"] is not None
    with api_client(tmp_path / "b", None) as client:
        assert client.get("/v1/health").json()["sources"]["meteofrance-aromepi"]["status"] == "unavailable"
    with api_client(tmp_path / "c", make_run(recent_run(minutes_ago=400)), max_age=120) as client:
        assert client.get("/v1/health").json()["sources"]["meteofrance-aromepi"]["status"] == "stale"


@pytest.mark.parametrize("hours_after", [0, 1, 5])
def test_no_step_is_served_after_the_last_one(tmp_path: Path, hours_after: int) -> None:
    # Pas ramené à l'heure pleine (`recent_run`) : le run de « il y a 5 h 20 » aurait plus de 6 h entre hh:00 et hh:20,
    # donc plus aucune échéance à servir (503 ; vu le 25/11/2026 à 17 h 16 UTC).
    run_time = datetime.now(UTC) - timedelta(hours=hours_after, minutes=20)
    with api_client(tmp_path, make_run(run_time), max_age=400) as client:
        resp = client.get("/v1/shortterm", params={"lat": PARIS[0], "lon": PARIS[1]})
        assert resp.status_code == 200
        assert len(resp.json()["steps"]) <= STEPS
