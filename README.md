# Météo-France Unified API

API météo résiliente et standardisée pour la France, conçue pour tourner sur **Oracle Cloud Always Free** (2 OCPU ARM64 / 12 Go RAM).

Elle remplace et fiabilise l'API officielle Météo-France en combinant :
1. **Open-Meteo auto-hébergé (France uniquement)** : modèles **AROME HD** (~1,5 km, 0–48 h) et **ARPEGE Europe** (~11 km, J+2 à J+4) fusionnés en une seule série horaire continue.
2. **Météo-France officielle (Portail API)** : la **pluie dans l'heure PIAF** (pas de 5 min), protégée par un cache persistant **SQLite WAL** (*Stale-While-Revalidate*) et un **Circuit Breaker**.

C'est la source météo interne de [Vigie](https://github.com/Axellum/vigie-api), qui reste la seule porte d'entrée (site, écrans, Home Assistant). Les **vigilances** et les **observations 6 min** de Météo-France sont lues par Vigie elle-même : elles ne passent plus par cette passerelle (retirées le 16/11/2026).

## Standards et normes appliqués
- **OpenAPI 3.1** (`/docs`, `/redoc`, `/openapi.json`)
- **RFC 9457** (`application/problem+json` pour toutes les erreurs HTTP)
- **WMO 4677** (codes météo normalisés) & **ISO-8601 UTC**
- **Aucune donnée inventée** : une information absente ou indisponible vaut `null`.

## Endpoints principaux

| Route | Usage |
| :--- | :--- |
| `GET /v1/weather?lat=48.85&lon=2.35` | Endpoint unifié (actuel, pluie 1h, horaire fusionné, journalier) |
| `GET /v1/compact?lat=48.85&lon=2.35` | Payload condensé (< 4 Ko) pour ESP32 / LVGL / écrans domotiques |
| `GET /v1/rain-nowcast?lat=48.85&lon=2.35` | Pluie dans l'heure PIAF (tranches de 5 min datées de leur fin ; repli AROME HD 15 min) : lue par Vigie |
| `GET /v1/rain-nowcast/frames` | Échéances (+5 à +60 min, pas de 5 min) du run PIAF déjà en mémoire : heure du run, boîte France, légende et chemin des tuiles (503 sans run chargé) |
| `GET /v1/rain-nowcast/tiles/{epoch}/{z}/{x}/{y}.png` | Tuile PNG 256 px (Web Mercator, zooms 3 à 10) d'une échéance PIAF en mémoire, même palette que le radar, transparente sans pluie ; `?run=` (le run lu dans `tile_path`) : immuable seulement si c'est le run qui l'a dessinée, sinon gardée 60 s, run dessiné dans `X-Piaf-Run` ; 404 hors run ou hors zooms, 503 sans run chargé |
| `GET /v1/forecast?...` | Proxy 100 % compatible avec la signature Open-Meteo ; avec `models=meteofrance_seamless` : AROME HD puis ARPEGE Europe, variable par variable (lu par Vigie pour la météo de la France) ; en-têtes `X-Cache-Status` (`fresh` ou `stale`) et `X-Cache-Age` |
| `GET /v1/shortterm?lat=48.85&lon=2.35` | Grêle, rafales, visibilité et type de précipitation des prochaines heures (jusqu'à 6 h, pas de 15 min) du modèle AROME-PI de Météo-France : une carte de la France par run en mémoire, 404 hors de la métropole et de la Corse, 503 sans run récent : lue par Vigie |
| `GET /v1/overseas?lat=14.6&lon=-61.0` | Prévision horaire AROME-OM de Météo-France pour un point d'outre-mer (température, humidité, vent, rafales, pression, nébulosité, pluie ; jusqu'à +48 h ; deux runs recollés) : 404 hors des fenêtres servies, 503 sans run récent : lue par Vigie |
| `GET /v1/station-daily?station=59343001&days=7` | Derniers jours mesurés par une station Météo-France (température minimale et maximale, pluie, vent moyen, rafale ; DPClim) : une commande par station et par jour au plus, hier après 11 h 30 UTC, 503 si le portail ne répond pas : lue par Vigie |
| `GET /v1/forests?department=11` | Météo des forêts (danger de feux de forêt par département, niveaux 1 à 4, J+1 et J+2) : `status` `available` de juin à septembre, `out_of_season` sinon : lue par Vigie |
| `GET /v1/probabilities?lat=48.85&lon=2.35` | Probabilités des prévisions d'ensemble de Météo-France : PE-AROME (tranches de 3 h jusqu'à +51 h : pluie ≥ 1 et 5 mm, neige, rafales, grêle, orage) et PE-ARPEGE (24 h jusqu'à +96 h : pluie ≥ 1, 5, 10 et 20 mm), les cartes du dernier run en mémoire ; 404 hors de la métropole et de la Corse, 503 sans run récent des deux |
| `GET /v1/radar/frames` | Images de la mosaïque radar Météo-France (lame d'eau, métropole, une toutes les 5 min, 3 h gardées) : dates, zone couverte, modèle de chemin des tuiles (= `/v1/radar/france/frames`) |
| `GET /v1/radar/{area}/frames` | La même chose pour une zone : `france`, `antilles`, `reunion` ou `new-caledonia` (404 sinon, 503 sans image) |
| `GET /v1/radar/tiles/{epoch}/{z}/{x}/{y}.png` | Tuile PNG 256 px (Web Mercator, zooms 3 à 10) d'une image radar de la métropole, classes d'intensité en couleurs, transparente sans pluie ; l'adresse porte la date de l'image : réponse immuable |
| `GET /v1/radar/{area}/tiles/{epoch}/{z}/{x}/{y}.png` | La même chose pour une zone d'outre-mer (ou `france`) |
| `GET /v1/health` | État de la base SQLite et des circuits breakers |

### Cache SQLite : appels partagés et purge

Les prévisions Open-Meteo passent par un cache SQLite (`app/cache.py`, durée de vie `METEO_FORECAST_TTL_SECONDS`, entrées périmées servies en panne jusqu'à `METEO_MAX_STALE_SECONDS`, 24 h par défaut). Quand plusieurs requêtes arrivent en même temps sur une clé froide ou périmée, **un seul appel part vers la source** (par clé et adresse) : les autres attendent sa réponse, et une panne n'est comptée qu'une fois par le disjoncteur. Une tâche de fond supprime toutes les 6 h (`METEO_CACHE_PURGE_INTERVAL_SECONDS`) les entrées plus vieilles que `METEO_MAX_STALE_SECONDS`, que plus rien ne peut servir ; les relevés DPClim et la météo des forêts, qui ont leur propre renouvellement, sont conservés.

### Radar (lame d'eau)

`DPRadar/v1` (mosaïques `METROPOLE`, `ANTILLES`, `REUNION` et `NOUVELLE-CALEDONIE`, observation `LAME_D_EAU`, maille 500 m, HDF5) ne rend que la dernière image de chaque zone : une tâche de fond la lit
dès qu'une nouvelle est publiée (date lue dans un petit document JSON avant le fichier de 2 Mo), la range sur disque (`radar/` à côté
de la base, ~230 Ko par image, `METEO_RADAR_KEEP_MINUTES`, 180 par défaut) et les tuiles sont calculées à la demande
(`app/radar_grid.py` : projection stéréographique polaire de la métropole et Mercator des trois zones d'outre-mer, écrites en numpy, pas de pyproj ; une zone a son dossier `radar/<zone>/`, la métropole garde `radar/`). Deux requêtes toutes les 5 minutes par zone, soit 8 pour les quatre. `METEO_RADAR_ENABLED=0` coupe les tâches, `METEO_RADAR_AREAS` (défaut `france,antilles,reunion,new-caledonia`) choisit les zones lues.
Source : Météo-France, Licence Ouverte 2.0 (jeu « Package Radar » de data.gouv.fr) ; crédit dans `/v1/radar/frames`.

### Tuiles pluie dans l'heure (PIAF +1 h)

Les routes `/v1/rain-nowcast/frames` et `/v1/rain-nowcast/tiles/{epoch}/{z}/{x}/{y}.png` réutilisent directement les grilles PIAF (`PiafRun`, pas de 5 min jusqu'à +60 min) déjà gardées en mémoire pour `/v1/rain-nowcast` : aucune requête supplémentaire vers le portail Météo-France. Les deux processus de la passerelle (`uvicorn --workers 2`) ont chacun leur carte en mémoire mais **ne téléchargent un run qu'une fois** : un verrou de fichier fait télécharger l'un des deux, qui range le run dans `piaf/` à côté de la base (`app/piaf_store.py`, un `.npz` compressé par run, les 2 derniers gardés, `METEO_PIAF_DIR` et `METEO_PIAF_KEEP_RUNS`), et l'autre le relit. Si ce dossier est inutilisable, chaque processus télécharge de son côté, comme avant. Les tuiles PNG 256 px (Web Mercator, zooms 3 à 10) sont rendues à la demande (`app/piaf_grid.py`) avec la même échelle d'intensité et la même palette que la mosaïque radar ; les deux routes répondent `503` tant qu'aucun run PIAF n'a été chargé en mémoire.
Source : Météo-France, prévision immédiate PIAF (`portail-api.meteofrance.fr`) ; crédit dans `/v1/rain-nowcast/frames`.

### AROME-PI (grêle, rafales, visibilité, type de précipitation)

`aromepi/1.0` (WCS `MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS`) : un run par heure, publié échéance après échéance, complet une vingtaine de minutes
après son heure (relevé le 18/11/2026) ; 24 échéances de 15 minutes (jusqu'à +6 h). Le WCS ne rend **qu'une échéance par requête** : un run coûte
4 produits x 24 échéances = 96 requêtes de la carte de la France (3,5 Mo chacune), espacées (`METEO_AROMEPI_REQUESTS_PER_MINUTE`, 20 par défaut)
pour laisser le quota de 50 par minute à PIAF et au radar. Chaque carte est réduite aussitôt à des blocs de 4 x 4 nœuds (0,04°, le pire du bloc :
maximum pour les rafales et la grêle, minimum pour la visibilité, nœud central pour le type de précipitation sauf verglas ou grêle qui l'emportent),
la grille d'un run tient dans 15 Mo ; les 3 derniers runs complets sont gardés sur disque (`aromepi/` à côté de la base) et un verrou de fichier
fait télécharger un seul des deux processus. Un run incomplet n'est jamais servi. `METEO_AROMEPI_ENABLED=0` coupe la tâche.
Mesure contre les stations au sol : `python tools/measure_aromepi.py --day AAAA-MM-JJ --hours 2-8` (clé dans `VIGIE_METEOFRANCE_API_KEY`).
Source : Météo-France, modèle AROME-PI, Licence Ouverte Etalab 2.0 (document de licence de l'API sur le portail, lu le 18/11/2026 ; voir l'ADR 0031 de vigie-api).

### Prévisions d'ensemble (PE-AROME, PE-ARPEGE : probabilités)

`pearome/1.0` : 4 runs par jour (03, 09, 15, 21 h UTC), échéances horaires jusqu'à +51 h, **probabilités déjà calculées** par Météo-France, une carte par seuil
et par échéance (0,8 Mo) : 7 produits x 17 tranches de 3 h = 119 requêtes par run, réduites à des blocs de 5 km (moyenne). `pearpege/1.0` : 4 runs par jour
(00, 06, 12, 18 h UTC), **un service par membre** (`PEARP000` à `PEARP034`, 35 membres) et des valeurs : le cumul de 24 h de chaque membre à +24, +48, +72
et +96 h (140 requêtes), et la probabilité d'un seuil est la part des membres qui le dépassent. Avec une emprise (`subset` de latitude et de longitude), ce WCS répond
`400 « Lat and long parameters are not allowed »` à une requête valable deux fois sur trois (jusqu'à 7 de suite) : PE-ARPEGE est donc lu **sans emprise** (toute l'Europe,
0,8 Mo par carte, 110 Mo par run) et découpé sur place ; PE-AROME garde l'emprise et rejoue ces 400 quand ils viennent. Requêtes espacées (`METEO_PEAROME_REQUESTS_PER_MINUTE`, 12 ; `METEO_PEARP_REQUESTS_PER_MINUTE`, 10),
un seul processus télécharge (verrou de fichier), les derniers runs complets sont gardés sur disque (`pearome/` et `pearp/` à côté de la base), un run incomplet
n'est jamais servi. `METEO_PEAROME_ENABLED=0` et `METEO_PEARP_ENABLED=0` coupent les tâches. Mesure contre les stations au sol :
`python tools/measure_ensemble.py --day AAAA-MM-JJ --ends 3-9` (clé dans `VIGIE_METEOFRANCE_API_KEY`).
Source : Météo-France, prévisions d'ensemble PE-AROME et PE-ARPEGE, Licence Ouverte Etalab 2.0 (document de licence de chaque API sur le portail, lu le 18/11/2026) :
la source et la date du run sont à citer, les données ne sont pas altérées (voir l'ADR 0032 de vigie-api).

### AROME-OM (prévisions de l'outre-mer)

`DPPaquetAROME-OM/v1`, paquet `SP1` : un modèle par territoire (Antilles, Guyane, océan Indien, Nouvelle-Calédonie, Polynésie), grille de 0,025°, 4 runs par jour
(00, 06, 12, 18 h UTC), 49 échéances horaires (+0 h à +48 h), **un fichier GRIB2 CCSDS de 1,5 à 12 Mo par échéance et par territoire** (245 requêtes et ~1,1 Go par run,
espacées par `METEO_AROMEOM_REQUESTS_PER_MINUTE`, 10 par défaut). Un run est listé 6 h 20 après son heure et complet vers 8 h (relevé le 18/11/2026) : le délai par défaut est de 8 h 30.
Seules des **fenêtres** autour des îles sont gardées (`app/aromeom_grid.py` : Guadeloupe et Martinique, Saint-Martin et Saint-Barthélemy, Guyane, La Réunion, Mayotte,
Nouvelle-Calédonie, îles de la Société, Tuamotu de l'ouest, Australes ; dessinées à la main, sans masque terre-mer) : 80 Mo par run sur disque (`aromeom/`, 2 runs gardés),
lus en `mmap`. La pluie du modèle est un cumul depuis le début du run : la pluie horaire est la différence de deux échéances. Les Marquises, Gambier, Wallis-et-Futuna,
Saint-Pierre-et-Miquelon et les TAAF ne sont pas couverts. `METEO_AROMEOM_ENABLED=0` coupe la tâche. Décodage CCSDS : `imagecodecs` (libaec).
Source : Météo-France, modèle AROME Outre-mer, Licence Ouverte Etalab 2.0 (document de licence de l'API sur le portail, lu le 18/11/2026).

### Relevés de stations (DPClim) et Météo des forêts

`DPClim/v1` est asynchrone : `commande-station/quotidienne` (202 et un numéro) puis `commande/fichier` (204 tant que ce n'est pas prêt, 201 avec le CSV ; 1,6 s relevé le 18/11/2026). Hier n'est disponible qu'après 11 h 30 UTC. Le cache garde, par station, les 14 derniers jours (base SQLite, clé `dpclim:<id>`) et n'est refait qu'une fois par jour ; les commandes sont étalées (`METEO_DPCLIM_COMMANDS_PER_MINUTE`, 6 par défaut et par processus) et une commande qui attendrait plus de `METEO_DPCLIM_MAX_WAIT_SECONDS` est refusée (503). Une valeur au code qualité 2 (« douteuse ») est écartée et nommée dans `doubtful`. `METEO_DPCLIM_ENABLED=0` coupe la route.

`DPMeteoForets/v1` : le fichier `carte/encours` (CSV des 96 départements) n'existe que de juin à septembre ; hors saison le portail répond 404, la route répond `out_of_season` et la santé reste « fresh » (contrôle toutes les 6 h, toutes les 30 min en saison). Le format est celui de l'échantillon publié par Météo-France sur data.gouv.fr (22/05/2024) : **le fichier servi aujourd'hui n'a pas pu être lu hors saison**. `METEO_FORESTS_ENABLED=0` coupe la tâche.
Source : Météo-France, données climatologiques de base et Météo des forêts, Licence Ouverte Etalab 2.0 (document de licence de chaque API sur le portail, lu le 18/11/2026).

## Développement & Tests locaux (Windows PowerShell)

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
```

## Déploiement sur Oracle Cloud (2 OCPU ARM64 / 12 Go RAM)

1. Ouvrir les ports `80` et `443` dans la *Security List* OCI.
2. Sur la VM Ubuntu/Debian ARM64 :
   ```bash
   git clone <votre-repo> meteo_france_api
   cd meteo_france_api/deploy/oracle
   chmod +x setup.sh && ./setup.sh
   # Renseigner .env (METEOFRANCE_API_KEY, METEO_API_KEY) et Caddyfile (nom de domaine)
   docker compose up -d --build
   ```

## Open-Meteo pour le monde entier (profil « monde »)

En plus de la France, la pile peut servir Open-Meteo pour le monde entier (prévisions, probabilités, qualité de l'air),
avec les mêmes données que l'API publique (seau `s3://openmeteo`, CC BY 4.0) et le même code (AGPLv3).

Mesuré le 22/11/2026 sur une VM Ampere A1 (4 OCPU, 24 Go) :

| | |
|---|---|
| Place sur le disque | ~83 Go le 22/11 (dont 10,5 Go de relief à 90 m), puis +3 à 4 Go par jour tant que les vieux morceaux restaient (voir « Ménage des vieux morceaux ») ; volume passé à 200 Go le 24/11/2026 |
| Synchro en continu | ½ cœur, ~500 Mo de RAM ; un passage d'ECMWF 9 km prend ~25 min (disque à ~50 Mo/s) |
| Débit de l'API | ~7 req/s à froid (lecture du disque), 790 à 840 req/s une fois en cache (4,5 Go de cache) |
| Qualité | 0,99 °C d'écart moyen à 49 aéroports (METAR), contre 0,97 °C pour l'API publique |

Mise en service :

1. Créer un volume de bloc de 100 Go, le monter (par ex. `/mnt/om-monde`, ligne `nofail` dans `/etc/fstab`).
   Jamais sur le disque de démarrage : le script refuse.
2. Fichiers fixes, que la synchro n'apporte pas (relief, grilles d'ICON) :
   ```bash
   OM_WORLD_DATA=/mnt/om-monde/data ./monde_fichiers_fixes.sh
   ```
3. Démarrer le profil :
   ```bash
   OM_WORLD_DATA=/mnt/om-monde/data COMPOSE_PROFILES=monde docker compose up -d
   ```
4. Le port 8081 ne doit être ouvert qu'au tunnel du client (même règle que 8080) ; l'interface publique reste fermée.

Les modèles gardés sont dans les commandes de `open-meteo-world-sync` et de `open-meteo-world-sync-lourds` (ECMWF IFS
9 km et GFS 0,11°, les trois quarts du volume, synchronisés à part pour ne pas copier le rayonnement direct d'ECMWF ni
le diffus de GFS, qu'aucune requête de Vigie n'utilise alors qu'ICON a besoin des deux). Les variables ont été triées le
24/11/2026 par un essai champ par champ : une API d'essai, où la variable est masquée par un dossier vide, comparée à la
production sur les requêtes exactes de Vigie en 104 points. `lifted_index`, `uv_index_clear_sky`, le direct d'ECMWF et
le diffus de GFS 0,11° ne changeaient aucune valeur (≈ 5,7 Go) ; tout le reste changeait au moins un champ (code du
temps, rayonnement, température ressentie). Sans GFS 0,11°, l'UV et les températures max/min disparaissaient sur 83 %
des jours : il reste. ICON monde, UKMO monde et ARPEGE monde ont été
retirés : ils prenaient 35 Go sans changer l'écart mesuré. Sans ICON Europe et D2, l'écart montait en Allemagne et en
Suisse.

## Ménage des vieux morceaux d'Open-Meteo (France et monde)

Open-Meteo range chaque variable en morceaux de quelques jours (`chunk_N.om`). La synchro (`sync --past-days 2`) écrit
les nouveaux morceaux mais ne retire jamais les anciens : le 24/11/2026, 14 Go des 24 Go de la France et 2,5 Go du monde
n'étaient plus lus par personne, et le volume du monde grossissait de 3 à 4 Go par jour.

`om_menage_morceaux.sh` retire, dans chaque dossier de variable, les morceaux qu'un morceau plus récent a remplacés et que
la synchro n'a pas réécrits depuis 3 jours (`OM_MENAGE_JOURS`). Le dernier morceau d'une variable et les fichiers fixes
(`static/`, relief) ne sont jamais touchés. Un client ne demande que les 24 dernières heures (`past_hours=24` dans Vigie),
loin des 3 jours. L'API garde un fichier retiré ouvert jusqu'à 10 minutes après son dernier accès : la place revient
ensuite (constaté le 24/11/2026).

```bash
sudo install -m 0755 om_menage_morceaux.sh /usr/local/sbin/
sudo install -m 0644 om-menage-morceaux.service om-menage-morceaux.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now om-menage-morceaux.timer   # chaque jour à 05 h 20
sudo om_menage_morceaux.sh --a-blanc /var/lib/docker/volumes/oracle_open_meteo_data/_data /mnt/om-monde/data
journalctl -u om-menage-morceaux.service                                               # ce qui a été retiré
```
