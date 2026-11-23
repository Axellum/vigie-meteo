"""Cache persistant SQLite WAL et Circuit Breaker par fournisseur.

Permet le mode *Stale-While-Revalidate* : si un fournisseur amont (ex: Météo-France)
renvoie une erreur 429/502 ou expire, l'API continue de servir la dernière donnée
valide connue en l'indiquant explicitement (`status: "stale"`).
"""

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CacheStore:
    """Stockage clé/valeur JSON + état des circuit breakers sur SQLite WAL."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at_ts REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS circuit_state (
                    provider TEXT PRIMARY KEY,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    open_until_ts REAL NOT NULL DEFAULT 0,
                    last_success_ts REAL,
                    last_error TEXT
                )
                """
            )
            self._conn.commit()

    def close(self) -> None:
        """Ferme proprement la connexion SQLite."""
        with self._lock:
            self._conn.close()

    def ping(self) -> bool:
        """Vérifie que la base SQLite répond."""
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def get_cache(self, cache_key: str) -> tuple[Any, float] | None:
        """Retourne `(payload, updated_at_ts)` si la clé existe en cache, sinon `None`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json, updated_at_ts FROM cache_entries WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload_json"]), float(row["updated_at_ts"])

    def set_cache(self, cache_key: str, provider: str, payload: Any) -> float:
        """Enregistre ou met à jour une entrée en cache et retourne son horodatage UNIX."""
        now_ts = time.time()
        serialized = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cache_entries (cache_key, provider, payload_json, updated_at_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    provider = excluded.provider,
                    payload_json = excluded.payload_json,
                    updated_at_ts = excluded.updated_at_ts
                """,
                (cache_key, provider, serialized, now_ts),
            )
            self._conn.commit()
        return now_ts

    def purge_older_than(self, max_age_seconds: float, keep_providers: tuple[str, ...] = ()) -> int:
        """Supprime les entrées plus vieilles que `max_age_seconds` (hors `keep_providers`) et rend leur nombre.

        Au-delà de `METEO_MAX_STALE_SECONDS` une entrée ne peut plus être servie, même en panne de la source : elle ne sert plus à rien.
        """
        cutoff = time.time() - max_age_seconds
        marks = ",".join("?" for _ in keep_providers)
        sql = "DELETE FROM cache_entries WHERE updated_at_ts < ?" + (f" AND provider NOT IN ({marks})" if marks else "")
        with self._lock:
            removed = self._conn.execute(sql, (cutoff, *keep_providers)).rowcount
            self._conn.commit()
        return removed

    def is_circuit_open(self, provider: str) -> bool:
        """Indique si le circuit breaker d'un fournisseur est actuellement ouvert."""
        now_ts = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT open_until_ts FROM circuit_state WHERE provider = ?",
                (provider,),
            ).fetchone()
        if row is None:
            return False
        return float(row["open_until_ts"]) > now_ts

    def record_success(self, provider: str) -> None:
        """Réinitialise le compteur d'échecs d'un fournisseur après un appel réussi."""
        now_ts = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO circuit_state (provider, fail_count, open_until_ts, last_success_ts, last_error)
                VALUES (?, 0, 0, ?, NULL)
                ON CONFLICT(provider) DO UPDATE SET
                    fail_count = 0,
                    open_until_ts = 0,
                    last_success_ts = excluded.last_success_ts,
                    last_error = NULL
                """,
                (provider, now_ts),
            )
            self._conn.commit()

    def record_failure(
        self,
        provider: str,
        error_msg: str,
        fail_threshold: int,
        cooldown_seconds: int,
    ) -> None:
        """Incrémente les échecs et ouvre le circuit breaker si le seuil est atteint."""
        now_ts = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT fail_count FROM circuit_state WHERE provider = ?",
                (provider,),
            ).fetchone()
            current_fails = int(row["fail_count"]) if row else 0
            new_fails = current_fails + 1
            open_until = now_ts + cooldown_seconds if new_fails >= fail_threshold else 0.0
            self._conn.execute(
                """
                INSERT INTO circuit_state (provider, fail_count, open_until_ts, last_success_ts, last_error)
                VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    fail_count = excluded.fail_count,
                    open_until_ts = excluded.open_until_ts,
                    last_error = excluded.last_error
                """,
                (provider, new_fails, open_until, error_msg[:500]),
            )
            self._conn.commit()

    def get_provider_state(self, provider: str) -> dict[str, Any]:
        """Retourne l'état synthétique d'un fournisseur pour `/v1/health`."""
        now_ts = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT fail_count, open_until_ts, last_success_ts, last_error FROM circuit_state WHERE provider = ?",
                (provider,),
            ).fetchone()
        if row is None:
            return {
                "circuit_open": False,
                "last_success_at": None,
                "last_error": None,
            }
        last_success_ts = row["last_success_ts"]
        return {
            "circuit_open": float(row["open_until_ts"]) > now_ts,
            "last_success_at": datetime.fromtimestamp(last_success_ts, tz=UTC) if last_success_ts else None,
            "last_error": row["last_error"],
        }
