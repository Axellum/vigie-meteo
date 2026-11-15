"""Tests unitaires et d'intégration de l'API unifiée (sans réseau réel)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


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


def _sample_vigilance_payload() -> dict[str, Any]:
    return {
        "product": {
            "update_time": "2026-11-15T09:00:00Z",
            "periods": [
                {
                    "end_validity_time": "2026-11-16T00:00:00Z",
                    "timelaps": {
                        "domain_ids": [
                            {
                                "domain_id": "75",
                                "max_color_id": 3,
                                "phenomenon_items": [
                                    {
                                        "phenomenon_id": 3,
                                        "phenomenon_max_color_id": 3,
                                        "timelaps_items": [
                                            {
                                                "begin_time": "2026-11-15T12:00:00Z",
                                                "end_time": "2026-11-15T20:00:00Z",
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "domain_id": "40",
                                "max_color_id": 1,
                                "phenomenon_items": [],
                            },
                        ]
                    },
                }
            ],
        }
    }


def _sample_nowcast_payload() -> dict[str, Any]:
    return {
        "properties": {
            "forecast": [
                {"time": "2026-11-15T10:05:00Z", "rain_intensity": 1, "rain_intensity_description": "Temps sec"},
                {"time": "2026-11-15T10:15:00Z", "rain_intensity": 3, "rain_intensity_description": "Pluie modérée"},
            ]
        }
    }


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
        if "cartevigilance" in url:
            return httpx.Response(200, json=_sample_vigilance_payload())
        if "paquet/pluie" in url:
            return httpx.Response(200, json=_sample_nowcast_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        resp = client.get("/v1/weather", params={"lat": 48.8566, "lon": 2.3522, "dept": "75"})
        assert resp.status_code == 200
        data = resp.json()

        # Fusion AROME HD (0-48h) puis ARPEGE Europe (J+2)
        assert len(data["hourly"]) == 3
        assert data["hourly"][0]["source_model"] == "meteofrance_arome_france_hd"
        assert data["hourly"][0]["temperature_c"] == 12.4
        assert data["hourly"][2]["source_model"] == "meteofrance_arpege_europe"
        assert data["hourly"][2]["temperature_c"] == 15.2

        # Vigilance officielle intacte
        assert data["vigilance"]["status"] == "fresh"
        assert data["vigilance"]["department"]["color"] == "orange"
        assert data["vigilance"]["department"]["phenomena"][0]["name"] == "Orages"

        # Pluie dans l'heure
        assert data["rain_nowcast"]["status"] == "fresh"
        assert len(data["rain_nowcast"]["slices"]) == 2
        assert data["rain_nowcast"]["slices"][1]["rain_level"] == 3


def test_stale_while_revalidate_and_circuit_breaker_on_502(tmp_path: Path) -> None:
    mf_should_fail = False

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/meteofrance" in url:
            return httpx.Response(200, json=_sample_open_meteo_payload())
        if "cartevigilance" in url:
            if mf_should_fail:
                return httpx.Response(502, text="Bad Gateway secret-mf-token-123")
            return httpx.Response(200, json=_sample_vigilance_payload())
        if "paquet/pluie" in url:
            return httpx.Response(200, json=_sample_nowcast_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        # 1. Premier appel : tout est frais et mis en cache
        r1 = client.get("/v1/vigilances", params={"dept": "75"})
        assert r1.status_code == 200
        assert r1.json()["status"] == "fresh"

        # 2. On simule une panne 502 Météo-France et on force l'expiration TTL (10 min > 5 min TTL, < 24 h max_stale)
        mf_should_fail = True
        import time

        client.app.state.cache._conn.execute(
            "UPDATE cache_entries SET updated_at_ts = ?",
            (time.time() - 600,),
        )
        client.app.state.cache._conn.commit()

        r2 = client.get("/v1/vigilances", params={"dept": "75"})
        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "stale"
        assert body["departments"][0]["color"] == "orange"
        # Vérifie que le secret Météo-France est bien masqué dans last_error
        assert "secret-mf-token-123" not in (body["source"]["last_error"] or "")


def test_compact_endpoint_under_4kb_for_esp32(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/v1/meteofrance" in url:
            return httpx.Response(200, json=_sample_open_meteo_payload())
        if "cartevigilance" in url:
            return httpx.Response(200, json=_sample_vigilance_payload())
        if "paquet/pluie" in url:
            return httpx.Response(200, json=_sample_nowcast_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        resp = client.get("/v1/compact", params={"lat": 48.85, "lon": 2.35, "dept": "75"})
        assert resp.status_code == 200
        raw_bytes = len(resp.content)
        assert raw_bytes < 4096
        data = resp.json()
        assert data["temp_c"] == 12.4
        assert data["vig_lvl"] == 3
        assert data["rain_1h_max"] == 3


def test_vigilances_geojson_rfc7946(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "cartevigilance" in str(request.url):
            return httpx.Response(200, json=_sample_vigilance_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        resp = client.get("/v1/vigilances/geojson")
        assert resp.status_code == 200
        geo = resp.json()
        assert geo["type"] == "FeatureCollection"
        assert len(geo["features"]) == 2
        assert geo["features"][0]["type"] == "Feature"


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


def _sample_arome_15m_payload() -> dict[str, Any]:
    return {
        "latitude": 43.65,
        "longitude": -1.45,
        "minutely_15": {
            "time": ["2026-11-15T10:00", "2026-11-15T10:15", "2026-11-15T10:30", "2026-11-15T10:45"],
            "precipitation": [0.0, 0.25, 0.9, 2.0],
        },
    }


def _sample_paquet_obs_geojson() -> list[dict[str, Any]]:
    # Format officiel GeoJSON DPPaquetObs v2 (liste de Features Point [lon, lat])
    return [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-1.51, 43.47]},
            "properties": {
                "geo_id_insee": "64024001",
                "validity_time": "2026-11-15T10:00:00Z",
                "t": 289.15,
                "td": 285.15,
                "u": 77,
                "dd": 290,
                "ff": 5.5,
                "dxi10": 300,
                "fxi10": 9.0,
                "rr_per": 0.2,
                "pmer": 101540,
                "vv": 12000,
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [2.35, 48.85]},
            "properties": {
                "geo_id_insee": "75114001",
                "validity_time": "2026-11-15T10:00:00Z",
                "t": 283.15,
                "u": 82,
                "ff": 2.0,
                "rr_per": 0.0,
                "pmer": 102000,
            },
        },
    ]


def test_rain_nowcast_falls_back_to_arome_15m_when_paquet_pluie_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "paquet/pluie" in url:
            return httpx.Response(404)
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


def test_observations_v2_nearest_station_si_conversion_and_cache(tmp_path: Path) -> None:
    obs_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal obs_calls
        url = str(request.url)
        if "DPPaquetObs/v2/paquet/stations/infrahoraire-6m" in url:
            obs_calls += 1
            assert "format=geojson" in url
            assert request.headers.get("apikey") == "secret-mf-token-123"
            return httpx.Response(200, json=_sample_paquet_obs_geojson())
        if "/v1/meteofrance" in url and "minutely_15" in url:
            return httpx.Response(200, json=_sample_arome_15m_payload())
        if "/v1/meteofrance" in url:
            return httpx.Response(200, json=_sample_open_meteo_payload())
        return httpx.Response(404)

    with _make_client(tmp_path, handler) as client:
        # Proche de Biarritz/Anglet (43.48, -1.52) -> doit sélectionner 64024001 (< 5 km)
        r1 = client.get("/v1/observations", params={"lat": 43.48, "lon": -1.52})
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["source"]["status"] == "fresh"
        assert d1["station"]["station_id"] == "64024001"
        assert d1["station"]["temperature_c"] == 16.0
        assert d1["station"]["dew_point_c"] == 12.0
        assert d1["station"]["wind_speed_kmh"] == 19.8
        assert d1["station"]["wind_gusts_kmh"] == 32.4
        assert d1["station"]["pressure_msl_hpa"] == 1015.4
        assert d1["station"]["distance_km"] < 5.0
        assert obs_calls == 1

        # Deuxième coordonnée (Paris) réutilise le paquet national en cache SQLite
        r2 = client.get("/v1/observations", params={"lat": 48.85, "lon": 2.35})
        assert r2.status_code == 200
        assert r2.json()["station"]["station_id"] == "75114001"
        assert r2.json()["station"]["temperature_c"] == 10.0
        assert obs_calls == 1

        # Inclusion optionnelle dans /v1/weather + vérification rayon
        r3 = client.get("/v1/weather", params={"lat": 43.48, "lon": -1.52, "include_obs": "true"})
        assert r3.status_code == 200
        w3 = r3.json()
        assert w3["observation"]["station_id"] == "64024001"
        assert w3["sources"]["observation"]["status"] == "fresh"

        r_far = client.get("/v1/observations", params={"lat": 45.0, "lon": 0.0, "radius_km": 10})
        assert r_far.status_code == 200
        assert r_far.json()["station"] is None


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
        assert h.json()["sources"]["meteofrance-obs"]["status"] == "not_configured"

        obs = client.get("/v1/observations", params={"lat": 43.48, "lon": -1.52})
        assert obs.status_code == 200
        assert obs.json()["source"]["status"] == "not_configured"
        assert obs.json()["station"] is None

        now = client.get("/v1/rain-nowcast", params={"lat": 43.48, "lon": -1.52})
        assert now.status_code == 200
        assert now.json()["source_model"] == "meteofrance_arome_france_hd_15min"


