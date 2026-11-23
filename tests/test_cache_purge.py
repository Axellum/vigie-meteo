"""Purge du cache SQLite : les entrées que plus rien ne peut servir sont supprimées (sans réseau)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.cache import CacheStore
from app.config import Settings
from app.main import purge_cache_forever


def _fill(cache: CacheStore, rows: list[tuple[str, str, float]]) -> None:
    for key, provider, age in rows:
        cache.set_cache(key, provider, {"k": key})
        cache._conn.execute("UPDATE cache_entries SET updated_at_ts = ? WHERE cache_key = ?", (time.time() - age, key))
    cache._conn.commit()


def test_purge_removes_only_entries_older_than_the_stale_limit_and_spares_the_kept_providers(tmp_path: Path) -> None:
    cache = CacheStore(str(tmp_path / "cache.db"))
    _fill(
        cache,
        [
            ("om:frais", "open-meteo", 60),
            ("om:limite", "open-meteo", 86000),
            ("om:vieux", "open-meteo", 90000),
            ("obs:vieux", "meteofrance-obs", 8 * 86400),  # un fournisseur retiré de la passerelle
            ("dpclim:123", "meteofrance-dpclim", 3 * 86400),
            ("forests:latest", "meteofrance-forests", 5 * 86400),
        ],
    )
    removed = cache.purge_older_than(86400, keep_providers=("meteofrance-dpclim", "meteofrance-forests"))
    assert removed == 2
    kept = {row["cache_key"] for row in cache._conn.execute("SELECT cache_key FROM cache_entries")}
    assert kept == {"om:frais", "om:limite", "dpclim:123", "forests:latest"}
    assert cache.purge_older_than(86400) == 2  # sans exception : les deux relevés plus vieux partent aussi
    assert cache.purge_older_than(86400) == 0


async def test_the_background_task_purges_then_waits(tmp_path: Path) -> None:
    cfg = Settings(METEO_DB_PATH=str(tmp_path / "cache.db"), METEO_CACHE_PURGE_INTERVAL_SECONDS=3600)
    cache = CacheStore(cfg.db_path)
    _fill(cache, [("om:frais", "open-meteo", 10), ("om:vieux", "open-meteo", 2 * 86400)])
    task = asyncio.create_task(purge_cache_forever(cache, cfg))
    await asyncio.sleep(0.3)
    task.cancel()
    keys = {row["cache_key"] for row in cache._conn.execute("SELECT cache_key FROM cache_entries")}
    assert keys == {"om:frais"}
