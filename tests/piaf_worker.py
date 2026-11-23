"""Un « processus de la passerelle » pour tester le partage du run PIAF : `python -m tests.piaf_worker <dossier> <compteur>`.

Il charge le dernier run PIAF comme le fait un worker d'uvicorn (transport simulé, jamais le réseau) et note dans le
fichier compteur une ligne par requête PIAF qu'il a envoyée.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.cache import CacheStore
from app.collectors.meteofrance import MeteoFranceNowcastCollector
from app.config import Settings
from tests.piaf_grib import piaf_filled

NOW = datetime(2026, 11, 17, 21, 3, 0, tzinfo=UTC)


async def main(directory: str, counter: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        with open(counter, "a", encoding="utf-8") as handle:
            handle.write("requête\n")
        return httpx.Response(200, content=piaf_filled(0.0))

    cfg = Settings(
        METEO_DB_PATH=str(Path(directory) / f"cache-{Path(counter).stem}.db"),
        METEO_PIAF_DIR=str(Path(directory) / "piaf"),
        METEOFRANCE_API_KEY="secret-mf-token-123",
        METEO_PIAF_STEPS=6,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector = MeteoFranceNowcastCollector(client, CacheStore(cfg.db_path), cfg)
    run = await collector.ensure_run(now=NOW)
    print(run.run.isoformat() if run else "aucun")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
