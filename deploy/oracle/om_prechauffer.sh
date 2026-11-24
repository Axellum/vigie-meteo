#!/usr/bin/env bash
# Préchauffage d'Open-Meteo France : met en mémoire, dans le cache de l'API (open-meteo-api), les morceaux réécrits
# depuis OM_PRECHAUFFE_HEURES heures (48 par défaut : les 24 dernières heures et toute la prévision) des modèles que
# Vigie lit pour la météo de la France (≈ 5 Go).
#
# Pourquoi : la synchro écrit les fichiers dans son propre conteneur (limite de 2 Go), leur cache s'en va vite, et le
# premier point demandé ensuite lit le disque (~50 Mo/s). Mesuré le 24/11/2026 sur 40 points neufs : 293 ms en
# médiane, 722 ms au 9e décile, contre 8 ms une fois en mémoire. La mer (MFWAM) répondait déjà en 2,5 ms à froid :
# elle n'est pas préchauffée.
#
# Lancé DANS le conteneur de l'API (docker exec) : le cache est compté dans sa limite (12 Go), pas dans un autre
# groupe. Les fichiers sont vraiment lus (posix_fadvise(WILLNEED) n'en chargeait qu'un tiers, essayé le 24/11/2026) ;
# relire ce qui est déjà en mémoire ne coûte qu'une copie (~1 s pour 5 Go). Lancé toutes les 15 minutes par
# om-prechauffer.timer (voir le README).
set -euo pipefail

CONTENEUR="${OM_PRECHAUFFE_CONTENEUR:-open-meteo-api}"
HEURES="${OM_PRECHAUFFE_HEURES:-48}"
MODELES="${OM_PRECHAUFFE_MODELES:-meteofrance_arome_france_hd meteofrance_arome_france_hd_15min meteofrance_arpege_europe}"

# shellcheck disable=SC2086  # MODELES : une liste séparée par des espaces, volontairement découpée
docker exec "$CONTENEUR" python3 -I -c '
import os, sys, time
heures, modeles = float(sys.argv[1]), sys.argv[2:]
depuis = time.time() - heures * 3600
n = taille = 0
debut = time.monotonic()
tampon = bytearray(16 * 1024 * 1024)
for modele in modeles:
    for dossier, _, fichiers in os.walk(os.path.join("/app/data", modele)):
        for nom in fichiers:
            chemin = os.path.join(dossier, nom)
            if not (nom.startswith("chunk_") and nom.endswith(".om")) or os.path.getmtime(chemin) < depuis:
                continue
            with open(chemin, "rb", buffering=0) as f:
                while f.readinto(tampon):
                    pass
            n += 1
            taille += os.path.getsize(chemin)
print(f"{n} morceaux, {taille / 1e9:.2f} Go lus en {time.monotonic() - debut:.1f} s")
' "$HEURES" $MODELES
