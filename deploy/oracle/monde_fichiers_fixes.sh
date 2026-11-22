#!/usr/bin/env bash
# Fichiers fixes d'Open-Meteo pour le monde (profil « monde » du docker-compose) : la synchro ne les apporte pas.
# - copernicus_dem90 : relief à 90 m (10,5 Go, une fois). Sans lui, la température en montagne est fausse
#   (GFS : 1,84 °C d'écart moyen sans relief, 1,51 °C avec, contre 49 aéroports le 22/11/2026).
# - grilles fixes des variantes d'ICON (< 400 Mo) : sans elles, ICON Europe et D2 répondent 400
#   « Missing native ICON grid artifact », même si leurs données sont là.
# Idempotent : un fichier déjà présent avec la bonne taille n'est pas retéléchargé.
# Usage : OM_WORLD_DATA=/mnt/om-monde/data ./monde_fichiers_fixes.sh   (puis redémarrer open-meteo-world)
set -euo pipefail

DATA="${OM_WORLD_DATA:-/mnt/om-monde/data}"
BUCKET="https://openmeteo.s3.amazonaws.com"
PREFIXES=(
  "data/copernicus_dem90/static/"
  "data/dwd_icon/static/"
  "data/dwd_icon_global_native/static/"
  "data/dwd_icon_global_eps_native/static/"
  "data/dwd_icon_eu_eps_native/static/"
  "data/dwd_icon_d2_eps_native/static/"
  "data/dwd_icon_d2_ruc_native/static/"
)

# Jamais sur le disque de démarrage : le dossier doit être sur un point de montage à part.
mount_point=$(df --output=target "$(dirname "$DATA")" | tail -1)
if [ "$mount_point" = "/" ] && [ "${FORCE:-0}" != "1" ]; then
  echo "Refus : $DATA est sur le disque de démarrage (FORCE=1 pour passer outre)." >&2
  exit 1
fi

for prefix in "${PREFIXES[@]}"; do
  listing=$(curl -fsS "$BUCKET/?list-type=2&max-keys=1000&prefix=$prefix")
  keys=$(echo "$listing" | grep -o '<Key>[^<]*</Key>' | sed 's/<[^>]*>//g')
  sizes=$(echo "$listing" | grep -o '<Size>[^<]*</Size>' | sed 's/<[^>]*>//g')
  paste <(echo "$keys") <(echo "$sizes") | while IFS=$'\t' read -r key size; do
    [ -n "$key" ] || continue
    dest="$DATA/${key#data/}"
    if [ -f "$dest" ] && [ "$(stat -c %s "$dest")" = "$size" ]; then
      continue
    fi
    mkdir -p "$(dirname "$dest")"
    curl -fsS --retry 3 -o "$dest.part" "$BUCKET/$key"
    mv "$dest.part" "$dest"
    echo "+ $key"
  done
done
echo "Fichiers fixes à jour dans $DATA ($(du -sh "$DATA/copernicus_dem90" | cut -f1) de relief)."
