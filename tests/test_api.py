"""Tests unitaires et d'intégration de l'API unifiée (sans réseau réel)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.piaf_grib import piaf_filled


def _sample_open_meteo_payload() -> dict[str, Any]:
    return {
        "latitude": 48.86,
        "longitude": 2.34,
        "current": {
            "time": "2026-11-15T10:00",
            "temperature_2m_meteofrance_arome_france_hd": 12.4,
            "apparent_temperature_meteofrance_arome_france_hd": 10.8,
            "relative_humidity_2m_meteofrance_arome_france_hd": 78,
            "precipitation_meteofrance_arome_france_hd": 0.0,
            "wind_speed_10m_meteofrance_arome_france_hd": 14.2,
            "wind_gusts_10m_meteofrance_arome_france_hd": 26.0,
            "wind_direction_10m_meteofrance_arome_france_hd": 240,
            "surface_pressure_meteofrance_arome_france_hd": 1018.5,
            "cloud_cover_meteofrance_arome_france_hd": 45,
            "weather_code_meteofrance_arome_france_hd": 2,
            "is_day_meteofrance_arome_france_hd": 1,
        },
        "hourly": {
            "time": [
                "2026-11-15T10:00",
                "2026-11-15T11:00",
                "2026-11-17T12:00",
            ],
            "temperature_2m_meteofrance_arome_france_hd": [12.4, 13.1, None],
            "temperature_2m_meteofrance_arpege_europe": [12.0, 12.9, 15.2],
            "precipitation_meteofrance_arome_france_hd": [0.0, 0.2, None],
            "precipitation_meteofrance_arpege_europe": [0.0, 0.1, 1.4],
            "wind_speed_10m_meteofrance_arome_france_hd": [14.2, 15.0, None],
            "wind_speed_10m_meteofrance_arpege_europe": [13.0, 14.0, 22.0],
            "weather_code_meteofrance_arome_france_hd": [2, 61, None],
            "weather_code_meteofrance_arpege_europe": [2, 61, 63],
        },
        "daily": {
            "time": ["2026-11-15"],
            "temperature_2m_min_meteofrance_arpege_europe": [8.1],
            "temperature_2m_max_meteofrance_arpege_europe": [14.6],
            "precipitation_sum_meteofrance_arpege_europe": [1.2],
            "wind_speed_10m_max_meteofrance_arpege_europe": [18.5],
            "wind_gusts_10m_max_meteofrance_arpege_europe": [35.0],
            "uv_index_max_meteofrance_arpege_europe": [2.1],
            "weather_code_meteofrance_arpege_europe": [61],
            "sunrise_meteofrance_arpege_europe": ["2026-11-15T07:01"],
            "sunset_meteofrance_arpege_europe": ["2026-11-15T16:12"],
        },
    }


def _piaf_step(request: httpx.Request) -> int:
    subsets = request.url.params.get_list("subset")
    time_subset = next(s for s in subsets if s.startswith("time("))
    return int(time_subset[5:-1])


def _piaf_response(request: httpx.Request) -> httpx.Response:
    """PIAF simulé (une carte GRIB2 de toute la France par échéance) : sec, sauf 0,3 mm en 5 min (3,6 mm/h, pluie
    modérée) partout à l'échéance +10 min."""
    return httpx.Response(200, content=piaf_filled(0.3 if _piaf_step(request) == 600 else 0.0))


def _make_client(
    tmp_path: Path,
    handler,
    api_key: str | None = None,
    mf_key: str | None = "secret-mf-token-123",
    fail_threshold: int = 2,
) -> TestClient:
    transport = httpx.MockTransport(handler)
    async_client = httpx.AsyncClient(transport=transport)
    settings = Settings(
        METEO_DB_PATH=str(tmp_path / "test_cache.db"),
        METEO_API_KEY=api_key,
        METEOFRANCE_API_KEY=mf_key,
        OPEN_METEO_BASE_URL="http://open-meteo.local:8080",
        OPEN_METEO_FALLBACK_URL=None,
        METEO_CIRCUIT_FAIL_THRESHOLD=fail_threshold,
        METEO_CIRCUIT_COOLDOWN_SECONDS=60,
    )
    app = create_app(settings=settings, http_client=async_client)
    return TestClient(app)


def test_unified_weather_merges_arome_arpege_and_meteofrance(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/meteofrance" in url:
            return httpx.Response(200, json=_sample_open_meteo_payload())
        if "/pro/piaf/" in url:
            return _piaf_response(request)
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        resp = client.get("/v1/weather", params={"lat": 48.8566, "lon": 2.3522})
        assert resp.status_code == 200
        data = resp.json()

        # Fusion AROME HD (0-48h) puis ARPEGE Europe (J+2)
        assert len(data["hourly"]) == 3
        assert data["hourly"][0]["source_model"] == "meteofrance_arome_france_hd"
        assert data["hourly"][0]["temperature_c"] == 12.4
        assert data["hourly"][2]["source_model"] == "meteofrance_arpege_europe"
        assert data["hourly"][2]["temperature_c"] == 15.2

        # Vigilances et observations : lues par Vigie elle-même, plus par la passerelle
        assert "vigilance" not in data and "observation" not in data
        assert set(data["sources"]) == {"forecast", "rain_nowcast"}

        # Pluie dans l'heure
        assert data["rain_nowcast"]["status"] == "fresh"
        assert data["rain_nowcast"]["source_model"] == "meteofrance-piaf"
        assert len(data["rain_nowcast"]["slices"]) == 12
        assert data["rain_nowcast"]["slices"][1]["rain_level"] == 3
        assert data["rain_nowcast"]["slices"][1]["rain_mm_h"] == 3.6


def test_stale_while_revalidate_and_circuit_breaker_on_502(tmp_path: Path) -> None:
    mf_should_fail = False

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/pro/piaf/" in url:
            if mf_should_fail:
                return httpx.Response(502, text="Bad Gateway secret-mf-token-123")
            return _piaf_response(request)
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        # 1. Premier appel : PIAF frais, mis en cache
        r1 = client.get("/v1/rain-nowcast", params={"lat": 48.85, "lon": 2.35})
        assert r1.status_code == 200
        assert r1.json()["status"] == "fresh"

        # 2. Panne 502 de Météo-France et cache expiré (10 min > 5 min de TTL, < 24 h de max_stale)
        mf_should_fail = True
        import time

        client.app.state.cache._conn.execute(
            "UPDATE cache_entries SET updated_at_ts = ?",
            (time.time() - 600,),
        )
        client.app.state.cache._conn.commit()
        # La carte PIAF en mémoire a vieilli elle aussi (une heure : des runs plus récents existent) ; il faut la
        # recharger, et le WCS est en panne
        from datetime import timedelta

        piaf_run = client.app.state.now_collector._run
        piaf_run.loaded_at = time.time() - 3600
        piaf_run.run -= timedelta(hours=1)

        r2 = client.get("/v1/rain-nowcast", params={"lat": 48.85, "lon": 2.35})
        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "stale"
        assert body["source_model"] == "meteofrance-piaf"
        assert len(body["slices"]) == 12
        # Le secret Météo-France est masqué dans last_error
        health = client.get("/v1/health").json()
        assert "secret-mf-token-123" not in (health["sources"]["meteofrance-nowcast"]["last_error"] or "")


def test_compact_endpoint_under_4kb_for_esp32(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/meteofrance" in url:
            return httpx.Response(200, json=_sample_open_meteo_payload())
        if "/pro/piaf/" in url:
            return _piaf_response(request)
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        resp = client.get("/v1/compact", params={"lat": 48.85, "lon": 2.35})
        assert resp.status_code == 200
        raw_bytes = len(resp.content)
        assert raw_bytes < 4096
        data = resp.json()
        assert data["temp_c"] == 12.4
        assert "vig_lvl" not in data and "dept" not in data
        assert data["rain_1h_max"] == 3


def test_vigilance_and_observation_routes_are_gone(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"aucun appel amont attendu : {request.url}")

    with _make_client(tmp_path, handler) as client:
        for path in ("/v1/vigilances", "/v1/vigilances/geojson", "/v1/observations?lat=48.85&lon=2.35"):
            assert client.get(path).status_code == 404, path
        assert set(client.get("/v1/health").json()["sources"]) == {
            "open-meteo",
            "meteofrance-nowcast",
            "meteofrance-radar",
            "meteofrance-aromepi",
            "meteofrance-aromeom",
            "meteofrance-pearome",
            "meteofrance-pearp",
        }


def test_api_key_auth_and_rfc9457_problem_json(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sample_open_meteo_payload())

    with _make_client(tmp_path, handler, api_key="mon-secret-api") as client:
        # /v1/health reste public
        h = client.get("/v1/health")
        assert h.status_code == 200
        assert h.json()["api_key_required"] is True

        # Sans clé -> 401 RFC 9457
        r_unauth = client.get("/v1/weather", params={"lat": 48.85, "lon": 2.35})
        assert r_unauth.status_code == 401
        assert r_unauth.headers["content-type"].startswith("application/problem+json")
        problem = json.loads(r_unauth.content)
        assert problem["status"] == 401
        assert problem["title"] == "Unauthorized"

        # Avec X-API-Key -> 200
        r_ok = client.get(
            "/v1/weather",
            params={"lat": 48.85, "lon": 2.35},
            headers={"X-API-Key": "mon-secret-api"},
        )
        assert r_ok.status_code == 200


@pytest.mark.parametrize("bad_lat", [95.0, -91.0])
def test_validation_error_returns_rfc9457(tmp_path: Path, bad_lat: float) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with _make_client(tmp_path, handler) as client:
        resp = client.get("/v1/weather", params={"lat": bad_lat, "lon": 2.35})
        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/problem+json")
        assert resp.json()["status"] == 422


def test_proxy_forecast_and_marine_endpoints(tmp_path: Path) -> None:
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if "/v1/marine" in str(request.url):
            return httpx.Response(200, json={"current": {"wave_height": 0.94, "sea_surface_temperature": 16.6}})
        return httpx.Response(200, json=_sample_open_meteo_payload())

    with _make_client(tmp_path, handler) as client:
        r_fc = client.get("/v1/forecast", params={"latitude": 43.65, "longitude": -1.45, "current": "temperature_2m"})
        assert r_fc.status_code == 200
        assert any("/v1/meteofrance" in u for u in seen_urls)

        r_mar = client.get("/v1/marine", params={"latitude": 43.65, "longitude": -1.45, "current": "wave_height"})
        assert r_mar.status_code == 200
        assert r_mar.json()["current"]["wave_height"] == 0.94
        assert any("/v1/marine" in u for u in seen_urls)


def test_proxy_forecast_says_whether_the_answer_is_fresh_or_stale(tmp_path: Path) -> None:
    """Un client qui exige du frais (Vigie) doit pouvoir refuser une réponse servie depuis un cache périmé."""
    state = {"up": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            return httpx.Response(503)
        return httpx.Response(200, json=_sample_open_meteo_payload())

    params = {"latitude": 47.66, "longitude": -2.76, "models": "meteofrance_seamless", "current": "temperature_2m"}
    with _make_client(tmp_path, handler) as client:
        first = client.get("/v1/forecast", params=params)
        assert first.headers["x-cache-status"] == "fresh" and first.headers["x-cache-age"] == "0"
        assert first.json() == _sample_open_meteo_payload()  # the body is Open-Meteo's, unchanged

        # Open-Meteo goes down and the cached answer expires: the proxy still answers, and says so.
        state["up"] = False
        client.app.state.settings.forecast_ttl_seconds = 0
        stale = client.get("/v1/forecast", params=params)
        assert stale.status_code == 200 and stale.headers["x-cache-status"] == "stale"
        assert stale.json() == first.json()


def _sample_arome_15m_payload() -> dict[str, Any]:
    return {
        "latitude": 43.65,
        "longitude": -1.45,
        "minutely_15": {
            "time": ["2026-11-15T10:00", "2026-11-15T10:15", "2026-11-15T10:30", "2026-11-15T10:45"],
            "precipitation": [0.0, 0.25, 0.9, 2.0],
        },
    }


def test_rain_nowcast_falls_back_to_arome_15m_when_piaf_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/pro/piaf/" in url:
            return httpx.Response(503)
        if "/v1/meteofrance" in url and "minutely_15" in url:
            return httpx.Response(200, json=_sample_arome_15m_payload())
        if "/v1/meteofrance" in url:
            return httpx.Response(200, json=_sample_open_meteo_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        r_now = client.get("/v1/rain-nowcast", params={"lat": 43.65, "lon": -1.45})
        assert r_now.status_code == 200
        now_data = r_now.json()
        assert now_data["status"] == "fresh"
        assert now_data["source_model"] == "meteofrance_arome_france_hd_15min"
        assert [s["rain_level"] for s in now_data["slices"]] == [1, 2, 3, 4]
        assert now_data["slices"][1]["rain_mm_h"] == 1.0

        r_weather = client.get("/v1/weather", params={"lat": 43.65, "lon": -1.45})
        assert r_weather.status_code == 200
        w_data = r_weather.json()
        assert w_data["rain_nowcast"]["source_model"] == "meteofrance_arome_france_hd_15min"
        assert len(w_data["rain_nowcast"]["slices"]) == 4


def test_without_mf_key_uses_arome_15m_nowcast_and_reports_health(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/meteofrance" in url and "minutely_15" in url:
            return httpx.Response(200, json=_sample_arome_15m_payload())
        if "/v1/meteofrance" in url:
            return httpx.Response(200, json=_sample_open_meteo_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler, mf_key=None) as client:
        h = client.get("/v1/health")
        assert h.status_code == 200
        assert h.json()["sources"]["meteofrance-nowcast"]["status"] == "not_configured"

        now = client.get("/v1/rain-nowcast", params={"lat": 43.48, "lon": -1.52})
        assert now.status_code == 200
        assert now.json()["source_model"] == "meteofrance_arome_france_hd_15min"


def test_piaf_helpers_run_candidates_parse_and_levels() -> None:
    from datetime import UTC, datetime

    from app.collectors.meteofrance import (
        piaf_coverage_id,
        piaf_mm_5min,
        piaf_run_candidates,
        rain_slice_from_mm_5min,
    )

    now = datetime(2026, 11, 16, 20, 38, 12, tzinfo=UTC)
    runs = piaf_run_candidates(now, publish_delay_minutes=10)
    assert [r.strftime("%H:%M") for r in runs] == ["20:25", "20:20", "20:15"]
    assert piaf_coverage_id(runs[0]) == (
        "TOTAL_PRECIPITATION_RATE__GROUND_OR_WATER_SURFACE___2026-11-16T20.25.00Z_PT5M"
    )

    assert piaf_mm_5min(0.53997802734375) == pytest.approx(0.54, abs=1e-3)
    assert piaf_mm_5min(float("nan")) is None
    assert piaf_mm_5min(-1.0) is None
    assert piaf_mm_5min(600.0) is None

    # mm sur 5 min -> mm/h (x12), seuils identiques au repli AROME 15 min
    assert [rain_slice_from_mm_5min(now, v).rain_level for v in (0.0, 0.05, 0.3, 0.6)] == [1, 2, 3, 4]
    assert rain_slice_from_mm_5min(now, 0.3).rain_mm_h == 3.6


def test_piaf_skips_unpublished_run_reads_the_map_and_caches(tmp_path: Path) -> None:
    piaf_requests: list[httpx.Request] = []
    rejected_runs: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/pro/piaf/" in url:
            piaf_requests.append(request)
            coverage = request.url.params["coverageid"]
            # Le run le plus récent n'est pas encore publié : le WCS répond 404
            if not rejected_runs or coverage in rejected_runs:
                rejected_runs.add(coverage)
                return httpx.Response(404, text="<mw:fault/>")
            return _piaf_response(request)
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        r1 = client.get("/v1/rain-nowcast", params={"lat": 43.6712, "lon": -1.4288})
        assert r1.status_code == 200
        body = r1.json()
        assert body["status"] == "fresh"
        assert body["source_model"] == "meteofrance-piaf"
        assert len(body["slices"]) == 12
        assert [s["rain_level"] for s in body["slices"][:3]] == [1, 3, 1]

        # 1 run refusé + 12 échéances, hôte api.meteofrance.fr, en-tête apikey seul
        assert len(piaf_requests) == 13
        req = piaf_requests[-1]
        assert req.url.host == "api.meteofrance.fr"
        assert req.headers["apikey"] == "secret-mf-token-123"
        assert "authorization" not in req.headers
        # La carte entière (toute la France) en GRIB2 : aucun découpage par point dans la requête
        subsets = req.url.params.get_list("subset")
        assert not any(s.startswith(("lat(", "long(")) for s in subsets)
        assert req.url.params["format"] == "application/wmo-grib"
        steps = sorted(_piaf_step(r) for r in piaf_requests[1:])
        assert steps == [300 * i for i in range(1, 13)]

        # Les tranches suivent le run : +5 min, +10 min, …
        from datetime import datetime

        t0 = datetime.fromisoformat(body["slices"][0]["time"].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(body["slices"][1]["time"].replace("Z", "+00:00"))
        assert (t1 - t0).total_seconds() == 300

        # Deuxième appel au même point : servi par le cache, aucune requête PIAF
        r2 = client.get("/v1/rain-nowcast", params={"lat": 43.67, "lon": -1.43})
        assert r2.json()["slices"] == body["slices"]
        assert len(piaf_requests) == 13

        # D'autres points du même run : lus dans la carte en mémoire, toujours aucune requête PIAF
        for lat, lon in ((48.85, 2.35), (45.76, 4.84), (43.3, 5.37), (50.63, 3.06)):
            other = client.get("/v1/rain-nowcast", params={"lat": lat, "lon": lon}).json()
            assert other["source_model"] == "meteofrance-piaf"
            assert [s["rain_level"] for s in other["slices"][:3]] == [1, 3, 1]
        assert len(piaf_requests) == 13


def test_piaf_unreadable_value_falls_back_to_arome(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/pro/piaf/" in url:
            return httpx.Response(200, content=piaf_filled(600.0))  # au-delà de 500 mm en 5 min : valeur rejetée
        if "/v1/meteofrance" in url and "minutely_15" in url:
            return httpx.Response(200, json=_sample_arome_15m_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        body = client.get("/v1/rain-nowcast", params={"lat": 43.65, "lon": -1.45}).json()
        assert body["source_model"] == "meteofrance_arome_france_hd_15min"
        health = client.get("/v1/health").json()
        assert "PIAF" in (health["sources"]["meteofrance-nowcast"]["last_error"] or "")
