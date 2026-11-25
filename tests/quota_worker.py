"""Un « processus de la passerelle » pour tester le quota partagé : `python -m tests.quota_worker <fichier> <n> <limite> <fenêtre>`.

Il prend `n` places dans le fichier partagé, en vrai temps, et écrit sur la sortie standard la date de chaque place obtenue
(la date écrite dans le fichier, lue sous le verrou). Aucun réseau.
"""

from __future__ import annotations

import sys
import time

from app.quota import SharedQuota


def main(path: str, count: int, limit: int, window: float) -> None:
    quota = SharedQuota(path, limit=limit, reserve=0, window=window, min_gap=0.0)
    for _ in range(count):
        while (wait := quota.try_acquire()) > 0.0:
            time.sleep(wait)
        print(repr(quota.last_grant_ts), flush=True)
    quota.close()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]))
