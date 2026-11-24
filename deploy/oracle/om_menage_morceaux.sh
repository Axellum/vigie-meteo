#!/usr/bin/env bash
# Ménage des morceaux périmés d'Open-Meteo (fichiers chunk_N.om).
#
# Pourquoi : `sync --past-days 2` écrit les nouveaux morceaux mais ne retire jamais les anciens. Les données grossissent
# donc sans fin : +3 à 4 Go par jour pour le monde (mesuré du 22 au 24/11/2026), et 14 Go de vieux morceaux sur 24 Go
# pour la France le 24/11/2026.
#
# Règle : dans chaque dossier de variable, un chunk_N.om est retiré s'il existe un morceau plus récent (N plus grand)
# ET s'il n'a pas été réécrit depuis OM_MENAGE_JOURS jours (3 par défaut). Un morceau que la synchro ne réécrit plus ne
# contient que des heures passées ; Vigie ne demande que les 24 dernières heures (past_hours=24). Le dernier morceau
# d'une variable n'est jamais retiré, même si la synchro est arrêtée. Les fichiers fixes (static/, relief) ne sont pas
# des chunk_N.om et ne sont jamais touchés. Un fichier ouvert par l'API reste lisible jusqu'à ce qu'elle le ferme.
#
# Usage : om_menage_morceaux.sh [--a-blanc] <dossier de données> [...]
#   --a-blanc : liste ce qui serait retiré, sans rien retirer.
# Installé sur Oracle dans /usr/local/sbin, lancé chaque jour par om-menage-morceaux.timer (voir le README).
set -euo pipefail

JOURS="${OM_MENAGE_JOURS:-3}"
[[ "$JOURS" =~ ^[0-9]+$ ]] && [ "$JOURS" -ge 2 ] || { echo "OM_MENAGE_JOURS doit être un entier >= 2" >&2; exit 2; }
A_BLANC=0
if [ "${1:-}" = "--a-blanc" ]; then
  A_BLANC=1
  shift
fi
[ $# -ge 1 ] || { echo "usage : $0 [--a-blanc] <dossier de données> [...]" >&2; exit 2; }

octets_total=0
fichiers_total=0
for racine in "$@"; do
  if [ ! -d "$racine" ] || [ "$(realpath "$racine")" = "/" ]; then
    echo "ignoré (absent ou racine du système) : $racine"
    continue
  fi
  octets=0
  fichiers=0
  # Un dossier de variable = un dossier qui contient des chunk_N.om.
  while IFS= read -r dossier; do
    max=$(find "$dossier" -maxdepth 1 -type f -name 'chunk_*.om' -printf '%f\n' \
      | sed -n 's/^chunk_\([0-9][0-9]*\)\.om$/\1/p' | sort -n | tail -1)
    [ -n "$max" ] || continue
    while IFS= read -r -d '' f; do
      num=$(basename "$f" .om)
      num=${num#chunk_}
      [[ "$num" =~ ^[0-9]+$ ]] || continue
      [ "$num" -lt "$max" ] || continue
      taille=$(stat -c %s "$f")
      if [ "$A_BLANC" = 1 ]; then
        echo "à retirer : $f ($((taille / 1048576)) Mo, réécrit le $(date -r "$f" '+%d/%m %H:%M'))"
      else
        rm -f -- "$f"
      fi
      octets=$((octets + taille))
      fichiers=$((fichiers + 1))
    done < <(find "$dossier" -maxdepth 1 -type f -name 'chunk_*.om' -mmin +$((JOURS * 1440)) -print0)
  done < <(find "$racine" -type f -name 'chunk_*.om' -printf '%h\n' | sort -u)
  verbe=$([ "$A_BLANC" = 1 ] && echo "à retirer" || echo "retirés")
  echo "$racine : $fichiers morceaux $verbe, $((octets / 1048576)) Mo"
  octets_total=$((octets_total + octets))
  fichiers_total=$((fichiers_total + fichiers))
done
echo "total : $fichiers_total morceaux, $((octets_total / 1048576)) Mo$([ "$A_BLANC" = 1 ] && echo " (à blanc, rien retiré)")"
