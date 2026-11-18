"""AROME-OM : décodage des paquets multi-champs (CCSDS), fenêtres, séries horaires recollées, téléchargement et route `/v1/overseas`.

Les fichiers de test reprennent la structure relevée le 18/11/2026 dans le paquet `SP1` d'AROME-OM-ANTIL (945 x 529 nœuds à 0,025° du nord
vers le sud depuis 22,9° N et 284,7° E ; CCSDS 16 bits ; pluie en cumul depuis le début du run, rafale en maximum de l'heure). Les cartes sont
petites (la grille entière n'est pas utile aux tests) mais gardent la même origine et le même pas.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.aromeom_grid import STEPS, VARIABLES, AromeOmStore, crop_of, find_window, merged_series
from app.collectors.aromeom import KEYS, AromeOmCollector, extract_step, run_candidates, run_stamp
from app.collectors.grib2 import LatLonGrid
from app.collectors.grib2_fields import decode_fields
from app.config import Settings
from app.main import create_app
from tests.grib_fields import field_message

RUN = datetime(2026, 11, 18, 0, tzinfo=UTC)
ANTIL_NORTH, ANTIL_WEST = 22.9, 284.7
NI, NJ = 945, 529  # la carte entière du territoire : la découpe des fenêtres en dépend
POINT = (14.6, -61.0)  # la Martinique


def full_map(value: float) -> np.ndarray:
    return np.full((NJ, NI), value, dtype=np.float32)


def sp1(lead: int, temperature: float = 27.0, rain_cumul: float = 0.0, gust_ms: float = 10.0) -> bytes:
    """Un fichier `SP1` d'AROME-OM-ANTIL à l'échéance `lead` : les huit champs lus (rafale, nébulosité et pluie absentes à 0 h)."""
    fields = {
        "t": (full_map(temperature + 273.15), None),
        "rh": (full_map(70.0), None),
        "ff": (full_map(5.0), None),
        "dd": (full_map(90.0), None),
        "pmsl": (full_map(101350.0), None),  # 1 013,5 hPa, en Pa
    }
    if lead > 0:
        fields["gust"] = (full_map(gust_ms), 2)
        fields["cloud"] = (full_map(40.0), None)
        fields["rain"] = (full_map(rain_cumul), 1)
    out = b""
    for name, (values, stat) in fields.items():
        out += field_message(
            values, KEYS[name], RUN, lead, ANTIL_NORTH, ANTIL_WEST, stat=stat, span=lead if name == "rain" else 1,
            decimal=0 if name == "pmsl" else 2, reference_value=100000.0 if name == "pmsl" else 0.0,
        )  # fmt: skip
    return out


# ------------------------------------------------------------------------------------------------ le décodage


def test_decode_fields_reads_ccsds_and_describes_each_field() -> None:
    fields = list(decode_fields(sp1(3, rain_cumul=2.5)))
    by_key = {f.key: f for f in fields}
    assert set(by_key) == set(KEYS.values())
    rain = by_key[KEYS["rain"]]
    assert (rain.lead_hours, rain.stat, rain.range_hours) == (3, 1, 3)
    assert float(rain.grid.values[10, 10]) == pytest.approx(2.5, abs=0.01)
    assert by_key[KEYS["gust"]].stat == 2 and by_key[KEYS["gust"]].range_hours == 1
    temperature = by_key[KEYS["t"]]
    assert (temperature.lead_hours, temperature.stat) == (3, None)
    assert float(temperature.grid.values[0, 0]) == pytest.approx(300.15, abs=0.01)
    grid = temperature.grid
    assert grid.values.shape == (NJ, NI)
    assert grid.lat0 == pytest.approx(22.9 - 0.025 * (NJ - 1)) and grid.dlat == pytest.approx(0.025)  # lat0 = le sud : la ligne 0 est tournée
    assert grid.lon0 == pytest.approx(ANTIL_WEST)
    assert temperature.reference_time == RUN


def test_decode_fields_can_skip_unwanted_fields_and_refuses_a_truncated_file() -> None:
    data = sp1(1)
    only = list(decode_fields(data, {KEYS["t"]}))
    assert [f.key for f in only] == [KEYS["t"]]
    with pytest.raises(ValueError):
        list(decode_fields(data[:-300]))


def test_simple_packing_is_still_read() -> None:
    message = field_message(np.full((4, 4), 7.0, dtype=np.float32), KEYS["t"], RUN, 0, 10.0, 300.0, packing=0)
    (field,) = decode_fields(message)
    assert float(field.grid.values[0, 0]) == pytest.approx(7.0)


# ------------------------------------------------------------------------------------------------ les fenêtres


def test_windows_find_islands_and_leave_the_sea_and_metropolitan_france_out() -> None:
    assert find_window(14.6, -61.0).name == "guadeloupe-martinique"  # Fort-de-France
    assert find_window(-21.1, 55.5).name == "reunion"  # Saint-Denis
    assert find_window(-17.55, -149.55).name == "polynesie-societe"  # Papeete
    assert find_window(-22.3, 166.45).name == "nouvelle-caledonie"  # Nouméa
    assert find_window(4.9, -52.3).name == "guyane"  # Cayenne
    assert find_window(-12.8, 45.2).name == "mayotte"
    assert find_window(48.85, 2.35) is None
    assert find_window(0.0, 0.0) is None


def test_crop_of_a_window_inside_the_map_and_outside() -> None:
    grid = LatLonGrid(full_map(0.0), 22.9 - 0.025 * (NJ - 1), ANTIL_WEST, 0.025, 0.025)
    window = find_window(14.6, -61.0)
    crop = crop_of(grid, window)
    assert crop is not None
    assert crop.shape == (93, 61)  # 2,3° x 1,5° à 0,025° (bornes comprises)
    assert crop.lat0 == pytest.approx(14.3, abs=0.013) and crop.lon0 == pytest.approx(-62.1, abs=0.013)
    assert crop.lon0 < 0  # les longitudes de la fenêtre sont ramenées de -180 à 180
    far = find_window(-21.1, 55.5)
    assert crop_of(grid, far) is None  # La Réunion n'est pas dans la carte des Antilles


# ------------------------------------------------------------------------------------------------ une échéance


def test_extract_step_converts_units_and_keeps_the_rain_cumulative() -> None:
    from app.aromeom_grid import TERRITORIES

    out, crops = extract_step(sp1(6, temperature=27.0, rain_cumul=3.0, gust_ms=10.0), 6, TERRITORIES[0])
    assert set(out) == {"guadeloupe-martinique", "saint-martin-saint-barthelemy"}
    stack = out["guadeloupe-martinique"]
    assert stack.shape == (*crops["guadeloupe-martinique"].shape, len(VARIABLES))
    cell = dict(zip(VARIABLES, stack[5, 5], strict=True))
    assert cell["temperature_c"] == pytest.approx(27.0, abs=0.01)
    assert cell["humidity_pct"] == pytest.approx(70.0, abs=0.01)
    assert cell["wind_kmh"] == pytest.approx(18.0, abs=0.05)  # 5 m/s
    assert cell["gust_kmh"] == pytest.approx(36.0, abs=0.05)  # 10 m/s
    assert cell["pressure_hpa"] == pytest.approx(13.5, abs=0.1)  # 1 013,5 hPa, rangé moins 1 000
    assert cell["precip_mm"] == pytest.approx(3.0, abs=0.01)  # un cumul à ce stade


def test_extract_step_zero_has_no_gust_cloud_or_rain_and_a_wrong_lead_is_refused() -> None:
    from app.aromeom_grid import TERRITORIES

    out, _ = extract_step(sp1(0), 0, TERRITORIES[0])
    cell = dict(zip(VARIABLES, out["guadeloupe-martinique"][5, 5], strict=True))
    assert cell["temperature_c"] == pytest.approx(27.0, abs=0.01)
    assert np.isnan(cell["gust_kmh"]) and np.isnan(cell["cloud_cover_pct"]) and np.isnan(cell["precip_mm"])
    with pytest.raises(ValueError, match="échéance"):
        extract_step(sp1(4), 5, TERRITORIES[0])
    with pytest.raises(ValueError, match="absent"):
        only_temperature = field_message(full_map(300.0), KEYS["t"], RUN, 2, ANTIL_NORTH, ANTIL_WEST)
        extract_step(only_temperature, 2, TERRITORIES[0])  # l'échéance 2 doit avoir tous les champs


# ------------------------------------------------------------------------------------------------ le run, le disque, le recollage


def write_run(store: AromeOmStore, run: datetime, temperature: float, rain_hours: dict[int, float] | None = None) -> None:
    """Un run complet à la main : une fenêtre par nom, la Martinique avec `temperature` partout et la pluie des heures données."""
    from app.aromeom_grid import WINDOWS, Crop

    epoch = int(run.timestamp())
    work = store.work_dir(epoch)
    work.mkdir(parents=True, exist_ok=True)
    crops = {}
    for window in WINDOWS:
        crop = Crop(0, 3, 0, 3, (window.lat[0] + window.lat[1]) / 2 - 0.025, (window.lon[0] + window.lon[1]) / 2 - 0.025, 0.025, 0.025)
        array = np.full((STEPS, 3, 3, len(VARIABLES)), np.nan, dtype=np.float16)
        array[..., VARIABLES.index("temperature_c")] = temperature
        array[..., VARIABLES.index("pressure_hpa")] = 13.5
        array[1:, ..., VARIABLES.index("precip_mm")] = 0.0
        for lead, mm in (rain_hours or {}).items():
            array[lead, ..., VARIABLES.index("precip_mm")] = mm
        np.save(work / f"{window.name}.npy", array)
        crops[window.name] = crop
    store.finish(epoch, crops)


def test_store_lists_loads_purges_and_reads_a_point(tmp_path: Path) -> None:
    store = AromeOmStore(tmp_path / "aromeom", keep_runs=2)
    assert store.epochs() == [] and store.latest_runs() == []
    for hours in (0, 6, 12):
        write_run(store, RUN + timedelta(hours=hours), temperature=20.0 + hours)
    assert len(store.epochs()) == 3
    assert store.purge() == 1 and len(store.epochs()) == 2
    runs = store.latest_runs(2)
    assert [r.run for r in runs] == [RUN + timedelta(hours=12), RUN + timedelta(hours=6)]
    window = find_window(*POINT)
    lat = (window.lat[0] + window.lat[1]) / 2
    lon = (window.lon[0] + window.lon[1]) / 2
    hours = runs[0].hours(lat, lon)
    assert hours is not None and hours[3]["temperature_c"] == pytest.approx(32.0)
    assert hours[3]["pressure_hpa"] == pytest.approx(1013.5)
    assert hours[0]["precip_mm"] is None  # NaN : l'échéance 0 n'a pas de pluie
    assert runs[0].node_latlon(lat, lon) is not None
    assert runs[0].hours(48.85, 2.35) is None
    assert store.latest_runs(1)[0] is runs[0]  # le même objet tant qu'aucun run plus récent n'est écrit


def test_an_unfinished_run_is_never_listed_or_served(tmp_path: Path) -> None:
    store = AromeOmStore(tmp_path)
    (store.work_dir(int(RUN.timestamp()))).mkdir(parents=True)
    assert store.epochs() == [] and store.latest_runs() == []


def test_merged_series_takes_the_newest_run_for_each_hour_and_keeps_the_previous_gust(tmp_path: Path) -> None:
    store = AromeOmStore(tmp_path)
    write_run(store, RUN, temperature=20.0, rain_hours={4: 2.0})
    write_run(store, RUN + timedelta(hours=6), temperature=30.0)
    runs = store.latest_runs(2)
    window = find_window(*POINT)
    lat, lon = (window.lat[0] + window.lat[1]) / 2, (window.lon[0] + window.lon[1]) / 2
    series = dict(merged_series(runs, lat, lon))
    assert series[RUN + timedelta(hours=3)]["temperature_c"] == pytest.approx(20.0)  # avant le run récent : le précédent
    assert series[RUN + timedelta(hours=6)]["temperature_c"] == pytest.approx(30.0)  # le récent l'emporte
    assert series[RUN + timedelta(hours=4)]["precip_mm"] == pytest.approx(2.0)
    assert series[RUN + timedelta(hours=6)]["precip_mm"] == pytest.approx(0.0)  # le run récent n'a pas de pluie à +0 h : l'heure garde celle du run précédent
    assert max(series) == RUN + timedelta(hours=6 + 48)
    assert merged_series(runs, 48.85, 2.35) is None


def test_run_candidates_follow_the_publication_delay() -> None:
    at = datetime(2026, 11, 18, 8, 29, tzinfo=UTC)  # 510 min après 00 h : tout juste avant le délai
    assert run_candidates(at, 510)[0] == datetime(2026, 11, 17, 18, tzinfo=UTC)
    at = datetime(2026, 11, 18, 8, 31, tzinfo=UTC)
    assert run_candidates(at, 510) == [datetime(2026, 11, 18, 0, tzinfo=UTC), datetime(2026, 11, 17, 18, tzinfo=UTC)]
    assert run_stamp(RUN) == "2026-11-18T00:00:00Z"


# ------------------------------------------------------------------------------------------------ le téléchargement et la route


def settings_for(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        METEOFRANCE_API_KEY="k" * 40, METEO_API_KEY=None, METEO_DB_PATH=str(tmp_path / "cache.db"), METEO_AROMEOM_DIR=str(tmp_path / "aromeom"),
        METEO_AROMEOM_REQUESTS_PER_MINUTE=30,
    )


def test_collector_downloads_a_run_only_when_listed_and_stores_the_fifteenth_hour(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = settings_for(tmp_path)
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        params = dict(request.url.params)
        if "/product" not in request.url.path:
            return httpx.Response(200, json={"links": [{"time": "000H"}]})
        lead = int(params["time"][:3])
        if "ANTIL" not in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=sp1(lead, rain_cumul=1.0 * lead))

    async def go() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = AromeOmCollector(client, AromeOmStore(tmp_path / "aromeom"), cfg)
            monkeypatch.setattr(collector, "_paced", lambda url, params, timeout: client.get(url, params=params, headers=collector._headers()))
            now = (RUN + timedelta(hours=9)).timestamp()
            assert await collector.refresh(now) is False  # ANTIL est lu, GUYANE répond 404 : le run n'est pas complet, rien n'est servi
            assert collector.store.epochs() == [] and "échéance" in (collector.last_error or "")
            assert (collector.store.work_dir(int(RUN.timestamp())) / "guadeloupe-martinique.npy").is_file()
            before = len(asked)
            collector._last_try = 0.0
            assert await collector.refresh(now) is False
            antil_asked = [u for u in asked[before:] if "AROME-OM-ANTIL" in u and "/product" in u]
            assert antil_asked == []  # le territoire déjà lu n'est pas relu à la reprise

    import asyncio

    asyncio.run(go())
    assert any("referencetime=2026-11-18T00%3A00%3A00Z" in u for u in asked)


def test_route_serves_the_hours_and_refuses_outside_windows_and_without_a_run(tmp_path: Path) -> None:
    cfg = settings_for(tmp_path)
    app = create_app(cfg, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    run = now - timedelta(hours=9)
    run = run.replace(hour=run.hour - run.hour % 6)
    with TestClient(app) as client:
        assert client.get("/v1/overseas", params={"lat": 48.85, "lon": 2.35}).status_code == 404
        window = find_window(*POINT)
        lat, lon = (window.lat[0] + window.lat[1]) / 2, (window.lon[0] + window.lon[1]) / 2
        assert client.get("/v1/overseas", params={"lat": lat, "lon": lon}).status_code == 503
        write_run(app.state.aromeom.store, run, temperature=28.0, rain_hours={14: 1.5})
        reply = client.get("/v1/overseas", params={"lat": lat, "lon": lon})
        assert reply.status_code == 200
        body = reply.json()
        assert body["source_model"] == "meteofrance-arome-om" and body["utc_offset_s"] == -4 * 3600 and body["window"] == "guadeloupe-martinique"
        assert body["attribution"].startswith("Source : Météo-France")
        assert body["steps"][0]["temperature_c"] == pytest.approx(28.0)
        first = datetime.fromisoformat(body["steps"][0]["time"])
        assert first >= now - timedelta(hours=1)
        assert reply.headers["cache-control"] == "public, max-age=900"
        health = client.get("/v1/health").json()
        assert health["sources"]["meteofrance-aromeom"]["status"] == "fresh"
