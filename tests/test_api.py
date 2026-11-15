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

