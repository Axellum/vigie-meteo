"""Cache de `/v1/forecast` : 90 s au lieu de 15 min, et la réponse périmée reste servie quand Open-Meteo ne répond plus.

Aucun réseau : transport simulé ; l'âge d'une entrée se règle dans la base SQLite du cache.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from app.config import Settings
from tests.test_api import _make_client, _sample_open_meteo_payload

PARAMS = {"latitude": 47.66, "longitude": -2.76, "models": "meteofrance_seamless", "current": "temperature_2m"}


def age_entries(client, seconds: float) -> None:  # type: ignore[no-untyped-def]
    """Vieillit toutes les entrées du cache de `seconds` secondes."""
    cache = client.app.state.cache
    with cache._lock:
        cache._conn.execute("UPDATE cache_entries SET updated_at_ts = updated_at_ts - ?", (seconds,))
        cache._conn.commit()


def test_default_durations() -> None:
    cfg = Settings(_env_file=None)
    assert cfg.proxy_forecast_ttl_seconds == 90  # entre 1 et 2 minutes
    assert cfg.forecast_ttl_seconds == 900  # `/v1/weather` et `/v1/marine` gardent leur durée
    assert cfg.max_stale_seconds == 86400  # le secours « périmé » aussi


def test_forecast_is_kept_a_minute_and_a_half_not_fifteen_minutes(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = _sample_open_meteo_payload()
        payload["generation"] = len(calls)
        return httpx.Response(200, json=payload)

    with _make_client(tmp_path, handler) as client:
        first = client.get("/v1/forecast", params=PARAMS)
        assert first.headers["x-cache-status"] == "fresh" and len(calls) == 1

        age_entries(client, 80)  # 80 s : encore dans la durée, aucun appel de plus
        again = client.get("/v1/forecast", params=PARAMS)
        assert again.json()["generation"] == 1 and len(calls) == 1
        assert again.headers["x-cache-status"] == "fresh" and int(again.headers["x-cache-age"]) >= 80

        age_entries(client, 20)  # 100 s en tout : dépassé, Open-Meteo est relu (avec 15 min il ne l'aurait pas été)
        renewed = client.get("/v1/forecast", params=PARAMS)
        assert renewed.json()["generation"] == 2 and len(calls) == 2
        assert renewed.headers["x-cache-status"] == "fresh" and renewed.headers["x-cache-age"] == "0"


def test_the_stale_answer_is_still_served_when_open_meteo_is_down(tmp_path: Path) -> None:
    state = {"up": True}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_sample_open_meteo_payload()) if state["up"] else httpx.Response(503)

    with _make_client(tmp_path, handler) as client:
        first = client.get("/v1/forecast", params=PARAMS)
        state["up"] = False
        age_entries(client, 200)  # au-delà des 90 s, mais bien en deçà des 24 h de tolérance
        stale = client.get("/v1/forecast", params=PARAMS)
        assert stale.status_code == 200 and stale.json() == first.json()
        assert stale.headers["x-cache-status"] == "stale" and int(stale.headers["x-cache-age"]) >= 200

        age_entries(client, 86400)  # plus vieille que `METEO_MAX_STALE_SECONDS` : plus rien à servir
        assert client.get("/v1/forecast", params=PARAMS).status_code == 503


def test_marine_keeps_its_own_longer_duration(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"hourly": {"wave_height": [1.0]}})

    with _make_client(tmp_path, handler) as client:
        params = {"latitude": 47.6, "longitude": -3.0, "hourly": "wave_height"}
        client.get("/v1/marine", params=params)
        age_entries(client, 300)
        client.get("/v1/marine", params=params)
        assert len(calls) == 1  # 300 s : encore dans les 15 minutes de la marine
