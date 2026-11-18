"""Prévisions d'ensemble : réduction des cartes, runs sur disque, lecteurs PE-AROME et PE-ARPEGE (service factice) et route `/v1/probabilities`.

Les cartes de test ont la géométrie réelle des WCS (relevée le 18/11/2026) : PE-AROME 661 x 421 nœuds à 0,025° (de -6° E à 10,5° E, de 41 à
51,5° N), PE-ARPEGE 166 x 106 nœuds à 0,1° (même cadre).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.collectors.ensemble import (
    AROME_PRODUCTS,
    ARPEGE_HOURS,
    FLAKY_RETRIES,
    MEMBERS,
    PeAromeCollector,
    PeArpegeCollector,
    ProbabilityStore,
    run_candidates,
)
from app.collectors.grib2 import LatLonGrid
from app.config import Settings
from app.ensemble_grid import GridGeometry, ProbabilityRun, crop_to_box, pool_mean, to_percent
from app.main import create_app
from tests.piaf_grib import encode

PARIS = (48.85, 2.35)
PARIS_A = (48.8, 2.3)  # un nœud exact de la grille de 0,1° de PE-ARPEGE (Paris tombe à mi-chemin de deux nœuds)
AROME_NJ, AROME_NI = 421, 661
ARPEGE_NJ, ARPEGE_NI = 106, 166
AROME_STEP, ARPEGE_STEP = 0.025, 0.1
FLAKY_BODY = "<mw:fault><mw:description>Lat and long parameters are not allowed for the MF-NWP-GLOBAL-PEARP000-01-EUROPE-WCS model</mw:description></mw:fault>"


def arome_node(lat: float, lon: float) -> tuple[int, int]:
    return round((lat - 41.0) / AROME_STEP), round((lon + 6.0) / AROME_STEP)


# ------------------------------------------------------------------------------------------------ la réduction


def test_pool_mean_averages_the_blocks_and_pads_the_edges() -> None:
    values = np.arange(9, dtype=np.float32).reshape(3, 3)  # 3 n'est pas un multiple de 2 : bords prolongés
    out = pool_mean(values, 2)
    assert out.shape == (2, 2)
    assert out[0, 0] == pytest.approx((0 + 1 + 3 + 4) / 4)
    assert out[1, 1] == pytest.approx(8.0)  # le dernier nœud prolongé quatre fois
    assert pool_mean(values, 1).tolist() == values.tolist()


def test_percent_is_clamped_never_wrapped() -> None:
    assert to_percent(np.array([-0.4, 0.4, 49.6, 100.57, 250.0], dtype=np.float32)).tolist() == [0, 0, 50, 100, 100]


def test_geometry_finds_the_block_of_a_point_and_knows_its_edges() -> None:
    geometry = GridGeometry(41.0, 354.0, 0.025, 0.025, AROME_NJ, AROME_NI, 2)
    j, i = arome_node(*PARIS)
    assert geometry.cell(*PARIS) == (j // 2, i // 2)
    assert geometry.cell(41.0, -6.0) == (0, 0)
    assert geometry.cell(64.1, -21.9) is None  # Reykjavik
    assert geometry.cell(40.0, 2.0) is None


def test_candidates_follow_the_six_hourly_cycles_and_the_publication_delay() -> None:
    at = datetime(2026, 11, 18, 11, 5, tzinfo=UTC)
    # PE-AROME : 03, 09, 15, 21 h ; avec 120 minutes de délai, 09 h 05 -> le run de 09 h
    assert run_candidates(at, 120, 3) == [datetime(2026, 11, 18, 9, tzinfo=UTC), datetime(2026, 11, 18, 3, tzinfo=UTC)]
    # PE-ARPEGE : 00, 06, 12, 18 h ; avec 180 minutes de délai, 08 h 05 -> le run de 06 h
    assert run_candidates(at, 180, 0) == [datetime(2026, 11, 18, 6, tzinfo=UTC), datetime(2026, 11, 18, 0, tzinfo=UTC)]
    assert run_candidates(datetime(2026, 11, 18, 1, tzinfo=UTC), 120, 3)[0] == datetime(2026, 11, 17, 21, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ les runs sur disque


def make_run(run: datetime, rain: int = 80) -> ProbabilityRun:
    geometry = GridGeometry(41.0, 354.0, 0.025, 0.025, AROME_NJ, AROME_NI, 2)
    nj, ni = -(-AROME_NJ // 2), -(-AROME_NI // 2)
    hours = {p.key: p.hours for p in AROME_PRODUCTS}
    layers = {key: np.zeros((len(h), nj, ni), dtype=np.uint8) for key, h in hours.items()}
    j, i = (n // 2 for n in arome_node(*PARIS))
    layers["rain_1mm"][1, j, i] = rain  # la tranche qui finit à +6 h
    layers["hail"][2, j, i] = 15
    return ProbabilityRun(run, geometry, hours, layers)


def test_store_saves_loads_lists_purges_and_hands_over_the_newest(tmp_path: Path) -> None:
    store = ProbabilityStore(tmp_path / "pearome", keep_runs=2)
    assert store.epochs() == [] and store.latest() is None
    base = datetime(2026, 11, 18, 3, tzinfo=UTC)
    for k in range(3):
        store.save(make_run(base + timedelta(hours=6 * k), rain=70 + k))
    assert len(store.epochs()) == 3
    assert store.purge() == 1 and len(store.epochs()) == 2
    run = store.latest()
    assert run is not None and run.run == base + timedelta(hours=12)
    point = run.point(*PARIS)
    assert point is not None
    assert point["rain_1mm"][6] == 72 and point["rain_1mm"][3] == 0
    assert point["hail"][9] == 15
    assert sorted(point["rain_1mm"]) == list(range(3, 52, 3))
    assert run.point(64.1, -21.9) is None
    assert store.latest() is run  # le même objet tant qu'aucun run plus récent n'est écrit
    store.save(make_run(base + timedelta(hours=18)))  # écrit par l'autre processus
    newest = store.latest()
    assert newest is not None and newest.run == base + timedelta(hours=18)


def test_store_skips_an_unreadable_newest_file(tmp_path: Path) -> None:
    store = ProbabilityStore(tmp_path)
    base = datetime(2026, 11, 18, 3, tzinfo=UTC)
    store.save(make_run(base))
    (tmp_path / f"{int((base + timedelta(hours=6)).timestamp())}.npz").write_bytes(b"not a zip")
    run = store.latest()
    assert run is not None and run.run == base


# ------------------------------------------------------------------------------------------------ PE-AROME (service factice)


def _arome_map(key: str, hour: int) -> bytes:
    values = np.full((AROME_NJ, AROME_NI), 3.0)
    if key == "rain_1mm" and hour == 6:
        j, i = arome_node(*PARIS)
        values[j - 1 : j + 2, i - 1 : i + 2] = 90.0
    if key == "hail" and hour == 9:
        values[:, :] = 100.57  # le bruit de l'empaquetage de Météo-France dépasse 100
    return encode(values[::-1], la1=51.5, lo1=354.0, step=AROME_STEP, decimal=2)


class FakeAromeWcs:
    """Le WCS PE-AROME : `published` = dates de run listées, `broken` = (produit, heure) qui répondent 404."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.published: set[str] | None = None
        self.broken: set[tuple[str, int]] = set()
        self.fail_all = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_all:
            return httpx.Response(502, text="bad gateway secret-mf-token-123")
        coverage = request.url.params["coverageid"]
        if self.published is not None and not any(stamp in coverage for stamp in self.published):
            return httpx.Response(404, text="<mw:fault/>")
        if request.url.path.endswith("/DescribeCoverage"):
            return httpx.Response(200, text="<wcs:CoverageDescriptions/>")
        key = next(p.key for p in AROME_PRODUCTS if coverage.startswith(p.coverage.split("___")[0]))
        hour = int(next(s for s in request.url.params.get_list("subset") if s.startswith("time("))[5:-1]) // 3600
        if (key, hour) in self.broken:
            return httpx.Response(404, text="<mw:fault/>")
        return httpx.Response(200, content=_arome_map(key, hour))


def arome_collector(tmp_path: Path, wcs: FakeAromeWcs, key: str | None = "secret-mf-token-123") -> PeAromeCollector:
    cfg = Settings(METEO_DB_PATH=str(tmp_path / "cache.db"), METEOFRANCE_API_KEY=key, METEO_PEAROME_CONCURRENCY=4)
    cfg.pearome_requests_per_minute = 60_000  # le vrai espacement est réglé pour le quota : pas d'attente en test
    client = httpx.AsyncClient(transport=httpx.MockTransport(wcs))
    return PeAromeCollector(client, ProbabilityStore(tmp_path / "pearome"), cfg)


NOW = datetime(2026, 11, 18, 11, 5, tzinfo=UTC).timestamp()  # le run de 09 h (délai de 120 min) est dû


async def test_a_pe_arome_run_costs_119_maps_and_a_point_reads_them(tmp_path: Path) -> None:
    wcs = FakeAromeWcs()
    collector = arome_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is True
    gets = [r for r in wcs.requests if r.url.path.endswith("/GetCoverage")]
    assert len(gets) == 7 * 17 == 119
    assert len([r for r in wcs.requests if r.url.path.endswith("/DescribeCoverage")]) == 1
    assert {r.url.params["format"] for r in gets} == {"application/wmo-grib"}
    assert {"long(-6,10.5)", "lat(41,51.5)"} <= set(gets[0].url.params.get_list("subset"))
    assert all("secret" not in str(r.url) for r in wcs.requests)  # la clé voyage dans un en-tête, jamais dans l'adresse
    run = collector.store.latest()
    assert run is not None and run.run == datetime(2026, 11, 18, 9, tzinfo=UTC)
    point = run.point(*PARIS)
    assert point is not None
    assert point["rain_1mm"][6] == 90 and point["rain_1mm"][3] == 3 and point["rain_1mm"][9] == 3
    assert point["hail"][9] == 100  # 100,57 borné à 100
    far = run.point(43.3, 5.4)  # Marseille : le fond
    assert far is not None and far["rain_1mm"][6] == 3


async def test_pe_arome_nothing_is_asked_when_the_run_is_kept_or_not_due(tmp_path: Path) -> None:
    wcs = FakeAromeWcs()
    collector = arome_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is True
    asked = len(wcs.requests)
    assert await collector.refresh(NOW + 60) is False  # trop tôt pour réessayer
    assert await collector.refresh(NOW + 3600) is False  # le run de 09 h est déjà gardé, celui de 15 h n'est pas dû
    assert len(wcs.requests) == asked


async def test_pe_arome_an_unpublished_run_is_probed_then_the_previous_one_is_taken(tmp_path: Path) -> None:
    wcs = FakeAromeWcs()
    wcs.published = set()
    collector = arome_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is False
    assert len(wcs.requests) == 2 and all(r.url.path.endswith("/DescribeCoverage") for r in wcs.requests)  # 09 h et 03 h, aucune carte
    wcs.published = {"2026-11-18T03.00.00Z"}
    assert await collector.refresh(NOW + 200) is True
    run = collector.store.latest()
    assert run is not None and run.run == datetime(2026, 11, 18, 3, tzinfo=UTC)


async def test_pe_arome_an_incomplete_run_is_never_served_and_the_retry_skips_what_was_read(tmp_path: Path) -> None:
    wcs = FakeAromeWcs()
    wcs.broken = {("storm", 30)}
    collector = arome_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is False
    assert collector.store.latest() is None
    assert collector.last_error is not None and "pas encore publiée" in collector.last_error
    first = len(wcs.requests)
    wcs.broken = set()
    assert await collector.refresh(NOW + 200) is True
    assert len(wcs.requests) - first < 119 // 2 + 10  # les échéances déjà lues ne sont pas redemandées
    run = collector.store.latest()
    assert run is not None and run.point(*PARIS)["rain_1mm"][6] == 90  # type: ignore[index]


async def test_pe_arome_a_failing_source_leaves_the_kept_run_and_hides_the_key(tmp_path: Path) -> None:
    wcs = FakeAromeWcs()
    collector = arome_collector(tmp_path, wcs)
    assert await collector.refresh(NOW) is True
    wcs.fail_all = True
    assert await collector.refresh(NOW + 7 * 3600) is False  # le run de 15 h est dû mais la source répond 502
    assert collector.last_error is not None and "secret-mf-token-123" not in collector.last_error
    assert collector.store.latest() is not None and collector.store.latest().run == datetime(2026, 11, 18, 9, tzinfo=UTC)  # type: ignore[union-attr]


async def test_pe_arome_without_a_key_does_nothing(tmp_path: Path) -> None:
    wcs = FakeAromeWcs()
    collector = arome_collector(tmp_path, wcs, key=None)
    assert collector.enabled() is False
    assert await collector.refresh(NOW) is False
    assert wcs.requests == []


# ------------------------------------------------------------------------------------------------ PE-ARPEGE (35 membres)


def _member_map(member: int, hour: int) -> bytes:
    values = np.zeros((ARPEGE_NJ, ARPEGE_NI))
    j, i = round((PARIS_A[0] - 41.0) / ARPEGE_STEP), round((PARIS_A[1] + 6.0) / ARPEGE_STEP)
    if hour == 48:  # les 35 membres, de 0 à 34 mm : le membre m cumule m mm à Paris
        values[j, i] = float(member)
    values[0, 0] = 25.0 if member < 7 else 0.0  # un coin où 7 membres sur 35 dépassent 20 mm
    return encode(values[::-1], la1=51.5, lo1=354.0, step=ARPEGE_STEP, decimal=2)


class FakePearpWcs:
    """Le WCS PE-ARPEGE : un service par membre dans l'adresse ; `flaky` = nombre de 400 intermittents à répondre avant la bonne réponse."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.flaky = 0
        self.published = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.published:
            return httpx.Response(404, text="<mw:fault/>")
        if request.url.path.endswith("/DescribeCoverage"):
            return httpx.Response(200, text="<wcs:CoverageDescriptions/>")
        if self.flaky > 0:
            self.flaky -= 1
            return httpx.Response(400, text=FLAKY_BODY)
        member = int(request.url.path.split("PEARP")[1][:3])
        hour = int(next(s for s in request.url.params.get_list("subset") if s.startswith("time("))[5:-1]) // 3600
        return httpx.Response(200, content=_member_map(member, hour))


def arpege_collector(tmp_path: Path, wcs: FakePearpWcs) -> PeArpegeCollector:
    cfg = Settings(METEO_DB_PATH=str(tmp_path / "cache.db"), METEOFRANCE_API_KEY="secret-mf-token-123", METEO_PEARP_CONCURRENCY=4)
    cfg.pearp_requests_per_minute = 60_000
    client = httpx.AsyncClient(transport=httpx.MockTransport(wcs))
    return PeArpegeCollector(client, ProbabilityStore(tmp_path / "pearp", keep_runs=2), cfg)


NOW_PEARP = datetime(2026, 11, 18, 9, 5, tzinfo=UTC).timestamp()  # le run de 06 h (délai de 180 min) est dû


async def test_pe_arpege_counts_the_members_above_each_threshold(tmp_path: Path) -> None:
    wcs = FakePearpWcs()
    collector = arpege_collector(tmp_path, wcs)
    assert await collector.refresh(NOW_PEARP) is True
    gets = [r for r in wcs.requests if r.url.path.endswith("/GetCoverage")]
    assert len(gets) == MEMBERS * len(ARPEGE_HOURS) == 140
    assert {r.url.path.split("/wcs/")[1].split("/")[0] for r in gets} == {f"MF-NWP-GLOBAL-PEARP{m:03d}-01-EUROPE-WCS" for m in range(MEMBERS)}
    assert all(r.url.params["coverageid"].endswith("_P1D") and "2026-11-18T06.00.00Z" in r.url.params["coverageid"] for r in gets)
    run = collector.store.latest()
    assert run is not None and run.run == datetime(2026, 11, 18, 6, tzinfo=UTC)
    point = run.point(*PARIS_A)
    assert point is not None
    # à +48 h, le membre m cumule m mm : au moins 1 mm = 34 membres sur 35, 5 mm = 30, 10 mm = 25, 20 mm = 15
    assert point["rain_1mm"][48] == round(100 * 34 / 35) and point["rain_5mm"][48] == round(100 * 30 / 35)
    assert point["rain_10mm"][48] == round(100 * 25 / 35) and point["rain_20mm"][48] == round(100 * 15 / 35)
    assert point["rain_1mm"][24] == 0 and point["rain_20mm"][72] == 0
    corner = run.point(41.0, -6.0)
    assert corner is not None and corner["rain_20mm"][24] == 20  # 7 membres sur 35


async def test_pe_arpege_retries_the_intermittent_400_and_counts_each_member_once(tmp_path: Path) -> None:
    wcs = FakePearpWcs()
    wcs.flaky = 12
    collector = arpege_collector(tmp_path, wcs)
    assert await collector.refresh(NOW_PEARP) is True
    run = collector.store.latest()
    assert run is not None
    point = run.point(*PARIS_A)
    assert point is not None and point["rain_1mm"][48] == round(100 * 34 / 35)  # 12 rejeux n'ont compté aucun membre deux fois
    assert len([r for r in wcs.requests if r.url.path.endswith("/GetCoverage")]) == 140 + 12


async def test_pe_arpege_a_persistent_400_is_given_up_not_looped(tmp_path: Path) -> None:
    wcs = FakePearpWcs()
    wcs.flaky = 10_000
    collector = arpege_collector(tmp_path, wcs)
    assert await collector.refresh(NOW_PEARP) is False
    assert collector.store.latest() is None
    assert collector.last_error is not None
    gets = len([r for r in wcs.requests if r.url.path.endswith("/GetCoverage")])
    assert gets <= (FLAKY_RETRIES + 1) * 4 + 4  # quelques tâches (la concurrence) ont essayé, la première erreur arrête le reste


async def test_pe_arpege_an_unlisted_run_is_not_downloaded(tmp_path: Path) -> None:
    wcs = FakePearpWcs()
    wcs.published = False
    collector = arpege_collector(tmp_path, wcs)
    assert await collector.refresh(NOW_PEARP) is False
    assert all(r.url.path.endswith("/DescribeCoverage") for r in wcs.requests)


# ------------------------------------------------------------------------------------------------ la route


def api_client(tmp_path: Path, arome: ProbabilityRun | None, arpege: ProbabilityRun | None = None) -> TestClient:
    cfg = Settings(METEO_DB_PATH=str(tmp_path / "cache.db"), METEOFRANCE_API_KEY="secret-mf-token-123")
    if arome is not None:
        ProbabilityStore(tmp_path / "pearome").save(arome)
    if arpege is not None:
        ProbabilityStore(tmp_path / "pearp").save(arpege)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    return TestClient(create_app(settings=cfg, http_client=client))


def recent_arome_run(hours_ago: float = 2.0) -> datetime:
    """Le run de 03/09/15/21 h UTC le plus récent d'il y a au moins `hours_ago` heures."""
    return run_candidates(datetime.now(UTC), int(hours_ago * 60), 3, 1)[0]


def make_arpege_run(run: datetime) -> ProbabilityRun:
    geometry = GridGeometry(41.0, 354.0, 0.1, 0.1, ARPEGE_NJ, ARPEGE_NI, 1)
    hours = {key: ARPEGE_HOURS for key in ("rain_1mm", "rain_5mm", "rain_10mm", "rain_20mm")}
    layers = {key: np.zeros((len(ARPEGE_HOURS), ARPEGE_NJ, ARPEGE_NI), dtype=np.uint8) for key in hours}
    j, i = round((PARIS_A[0] - 41.0) / 0.1), round((PARIS_A[1] + 6.0) / 0.1)
    layers["rain_1mm"][1, j - 1 : j + 2, i - 1 : i + 2] = 66  # autour du nœud : Paris tombe à mi-chemin de deux nœuds
    return ProbabilityRun(run, geometry, hours, layers)


def test_route_serves_the_windows_not_yet_passed_with_their_credit(tmp_path: Path) -> None:
    run_time = recent_arome_run()
    arpege_time = run_candidates(datetime.now(UTC), 180, 0, 1)[0]
    with api_client(tmp_path, make_run(run_time), make_arpege_run(arpege_time)) as client:
        resp = client.get("/v1/probabilities", params={"lat": PARIS[0], "lon": PARIS[1]})
        assert resp.status_code == 200
        body = resp.json()
        arome, arpege = body["arome"], body["arpege"]
        assert arome["source_model"] == "meteofrance-pe-arome" and "Météo-France" in arome["attribution"] and arome["members"] == 35
        assert arpege["source_model"] == "meteofrance-pe-arpege" and "Météo-France" in arpege["attribution"]
        times = [datetime.fromisoformat(w["time"].replace("Z", "+00:00")) for w in arome["windows"]]
        assert times == sorted(times) and times[0] >= datetime.now(UTC) - timedelta(seconds=1)
        assert all(w["window_hours"] == 3 for w in arome["windows"]) and all(w["window_hours"] == 24 for w in arpege["windows"])
        assert set(arome["windows"][-1]["probability_pct"]) == {p.key for p in AROME_PRODUCTS}
        by_time = {t: w for t, w in zip(times, arome["windows"], strict=True)}
        spot = by_time.get(run_time + timedelta(hours=6))
        if spot is not None:  # la tranche du phénomène n'est pas encore passée selon l'heure du test
            assert spot["probability_pct"]["rain_1mm"] == 80
        day2 = next(w for w in arpege["windows"] if w["time"].startswith((arpege_time + timedelta(hours=48)).strftime("%Y-%m-%dT%H")))
        assert day2["probability_pct"]["rain_1mm"] == 66
        assert resp.headers["cache-control"] == "public, max-age=600"


def test_route_with_a_single_ensemble_outside_the_map_and_without_a_run(tmp_path: Path) -> None:
    with api_client(tmp_path / "a", make_run(recent_arome_run())) as client:
        body = client.get("/v1/probabilities", params={"lat": PARIS[0], "lon": PARIS[1]}).json()
        assert body["arome"] is not None and body["arpege"] is None  # PE-ARPEGE sans run : PE-AROME seul
        assert client.get("/v1/probabilities", params={"lat": 64.1, "lon": -21.9}).status_code == 404
        assert client.get("/v1/probabilities", params={"lat": 95, "lon": 2}).status_code == 422
    with api_client(tmp_path / "b", None) as client:
        assert client.get("/v1/probabilities", params={"lat": PARIS[0], "lon": PARIS[1]}).status_code == 503
    old = recent_arome_run(hours_ago=40)
    with api_client(tmp_path / "c", make_run(old)) as client:  # run de plus de 14 h : plus servi
        assert client.get("/v1/probabilities", params={"lat": PARIS[0], "lon": PARIS[1]}).status_code == 503


def test_health_says_whether_each_ensemble_is_fresh(tmp_path: Path) -> None:
    with api_client(tmp_path / "a", make_run(recent_arome_run())) as client:
        sources = client.get("/v1/health").json()["sources"]
        assert sources["meteofrance-pearome"]["status"] == "fresh" and sources["meteofrance-pearome"]["updated_at"] is not None
        assert sources["meteofrance-pearp"]["status"] == "unavailable"
    with api_client(tmp_path / "b", make_run(recent_arome_run(hours_ago=40))) as client:
        assert client.get("/v1/health").json()["sources"]["meteofrance-pearome"]["status"] == "stale"


def test_crop_to_box_keeps_the_box_and_its_edge_nodes_of_a_wider_grid() -> None:
    # une grille « Europe » de 20°N à 70°N et de -20° (340°) à 40° E, pas de 0,5°, dont chaque valeur dit sa position
    lats = 20.0 + 0.5 * np.arange(101)
    lons = np.arange(121) * 0.5 - 20.0
    values = (lats[:, None] * 1000.0 + lons[None, :]).astype(np.float32)
    grid = LatLonGrid(values, 20.0, 340.0, 0.5, 0.5)
    cut = crop_to_box(grid, 41.0, -6.0, 51.5, 10.5)
    assert (cut.lat0, cut.lon0) == (41.0, 354.0)
    assert cut.values.shape == (22, 34)  # 41..51,5 et -6..10,5 par 0,5 : bords compris
    assert cut.values[0, 0] == pytest.approx(41.0 * 1000 - 6.0) and cut.values[-1, -1] == pytest.approx(51.5 * 1000 + 10.5)
    assert cut.index(48.5, 2.5) is not None
    with pytest.raises(ValueError):
        crop_to_box(grid, 80.0, -6.0, 85.0, 10.5)
