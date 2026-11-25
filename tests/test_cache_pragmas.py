"""Réglages SQLite du cache : WAL, cache de pages et projection mémoire élargis, tris en mémoire, attente d'un verrou."""

from __future__ import annotations

from pathlib import Path

from app.cache import CacheStore


def pragma(store: CacheStore, name: str) -> object:
    with store._lock:
        return store._conn.execute(f"PRAGMA {name}").fetchone()[0]


def test_the_connection_is_set_up_for_wal_and_wide_caches(tmp_path: Path) -> None:
    store = CacheStore(str(tmp_path / "cache.db"))
    assert str(pragma(store, "journal_mode")).lower() == "wal"
    assert pragma(store, "synchronous") == 1  # NORMAL
    assert pragma(store, "cache_size") == -16384  # 16 Mo
    assert pragma(store, "mmap_size") == 268435456  # 256 Mo
    assert pragma(store, "temp_store") == 2  # MEMORY
    assert pragma(store, "busy_timeout") == 5000
    store.close()


def test_the_cache_still_stores_and_reads_back(tmp_path: Path) -> None:
    store = CacheStore(str(tmp_path / "cache.db"))
    payload = {"hourly": {"temperature_2m": [1.5, 2.5]}}
    stamp = store.set_cache("k", "open-meteo", payload)
    assert store.get_cache("k") == (payload, stamp)
    store.close()
