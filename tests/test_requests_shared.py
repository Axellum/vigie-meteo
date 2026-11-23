"""Requêtes simultanées partagées : un seul appel amont par clé froide ou périmée (sans réseau)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from app.cache import CacheStore
from app.collectors.base import ResilientCollector
from app.config import Settings

URL = "http://open-meteo.local:8080/v1/meteofrance"


class SlowUpstream:
    """Un amont lent (les requêtes simultanées se chevauchent) qui compte ses appels."""

    def __init__(self, delay: float = 0.05, status: int = 200) -> None:
        self.calls: list[httpx.Request] = []
        self.delay = delay
        self.status = status

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        await asyncio.sleep(self.delay)
        if self.status != 200:
            return httpx.Response(self.status, text="panne")
        return httpx.Response(200, json={"hourly": {"time": ["2026-11-23T10:00"]}, "n": len(self.calls)})


def make_collector(tmp_path: Path, upstream: SlowUpstream, **settings: object) -> ResilientCollector:
    cfg = Settings(METEO_DB_PATH=str(tmp_path / "cache.db"), **settings)
    collector = ResilientCollector(
        httpx.AsyncClient(transport=httpx.MockTransport(upstream)), CacheStore(cfg.db_path), cfg
    )
    collector.provider_name = "open-meteo"
    return collector


async def test_simultaneous_requests_on_a_cold_key_make_one_upstream_call(tmp_path: Path) -> None:
    upstream = SlowUpstream()
    collector = make_collector(tmp_path, upstream)
    results = await asyncio.gather(
        *(collector.fetch_with_cache("om:fr:1:2:4", URL, 900, params={"latitude": 1}) for _ in range(25))
    )
    assert len(upstream.calls) == 1
    assert all(payload == results[0][0] and status.status == "fresh" for payload, status in results)
    assert collector._in_flight == {}  # rien ne reste en vol : la requête suivante lira le cache


async def test_a_warm_key_makes_no_call_and_an_expired_one_makes_one(tmp_path: Path) -> None:
    upstream = SlowUpstream(delay=0.01)
    collector = make_collector(tmp_path, upstream)
    await collector.fetch_with_cache("k", URL, 900)
    await asyncio.gather(*(collector.fetch_with_cache("k", URL, 900) for _ in range(10)))
    assert len(upstream.calls) == 1  # le cache frais répond sans appel

    collector._cache._conn.execute("UPDATE cache_entries SET updated_at_ts = ?", (time.time() - 2000,))
    collector._cache._conn.commit()
    await asyncio.gather(*(collector.fetch_with_cache("k", URL, 900) for _ in range(10)))
    assert len(upstream.calls) == 2  # périmé : un appel pour les dix


async def test_different_keys_or_addresses_are_not_merged(tmp_path: Path) -> None:
    upstream = SlowUpstream()
    collector = make_collector(tmp_path, upstream)
    await asyncio.gather(
        collector.fetch_with_cache("a", URL, 900),
        collector.fetch_with_cache("b", URL, 900),
        collector.fetch_with_cache("a", URL + "/autre", 900),  # même clé, autre adresse (repli) : appel distinct
    )
    assert len(upstream.calls) == 3


async def test_a_failure_is_counted_once_and_shared(tmp_path: Path) -> None:
    upstream = SlowUpstream(status=502)
    collector = make_collector(tmp_path, upstream, METEO_CIRCUIT_FAIL_THRESHOLD=2)
    results = await asyncio.gather(*(collector.fetch_with_cache("k", URL, 900) for _ in range(12)))
    assert len(upstream.calls) == 1
    assert all(payload is None and status.status == "unavailable" for payload, status in results)
    assert not collector._cache.is_circuit_open("open-meteo")  # un seul échec compté, pas douze : le disjoncteur reste fermé


async def test_a_failure_serves_the_stale_cache_to_everyone(tmp_path: Path) -> None:
    upstream = SlowUpstream()
    collector = make_collector(tmp_path, upstream)
    await collector.fetch_with_cache("k", URL, 900)
    collector._cache._conn.execute("UPDATE cache_entries SET updated_at_ts = ?", (time.time() - 2000,))
    collector._cache._conn.commit()
    upstream.status = 502
    results = await asyncio.gather(*(collector.fetch_with_cache("k", URL, 900) for _ in range(8)))
    assert len(upstream.calls) == 2
    assert all(payload is not None and status.status == "stale" for payload, status in results)


async def test_the_others_still_get_the_answer_when_the_first_client_leaves(tmp_path: Path) -> None:
    upstream = SlowUpstream(delay=0.2)
    collector = make_collector(tmp_path, upstream)
    leader = asyncio.create_task(collector.fetch_with_cache("k", URL, 900))
    await asyncio.sleep(0.05)  # l'appel amont est parti
    followers = [asyncio.create_task(collector.fetch_with_cache("k", URL, 900)) for _ in range(5)]
    await asyncio.sleep(0.01)
    leader.cancel()  # le premier client se déconnecte
    results = await asyncio.gather(*followers)
    assert len(upstream.calls) == 1
    assert all(payload is not None and status.status == "fresh" for payload, status in results)
    assert collector._cache.get_cache("k") is not None
