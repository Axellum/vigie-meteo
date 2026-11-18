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
| `GET /v1/forecast?...` | Proxy 100 % compatible avec la signature Open-Meteo ; avec `models=meteofrance_seamless` : AROME HD puis ARPEGE Europe, variable par variable (lu par Vigie pour la météo de la France) ; en-têtes `X-Cache-Status` (`fresh` ou `stale`) et `X-Cache-Age` |
| `GET /v1/shortterm?lat=48.85&lon=2.35` | Grêle, rafales, visibilité et type de précipitation des prochaines heures (jusqu'à 6 h, pas de 15 min) du modèle AROME-PI de Météo-France : une carte de la France par run en mémoire, 404 hors de la métropole et de la Corse, 503 sans run récent : lue par Vigie |
| `GET /v1/overseas?lat=14.6&lon=-61.0` | Prévision horaire AROME-OM de Météo-France pour un point d'outre-mer (température, humidité, vent, rafales, pression, nébulosité, pluie ; jusqu'à +48 h ; deux runs recollés) : 404 hors des fenêtres servies, 503 sans run récent : lue par Vigie |
| `GET /v1/probabilities?lat=48.85&lon=2.35` | Probabilités des prévisions d'ensemble de Météo-France : PE-AROME (tranches de 3 h jusqu'à +51 h : pluie ≥ 1 et 5 mm, neige, rafales, grêle, orage) et PE-ARPEGE (24 h jusqu'à +96 h : pluie ≥ 1, 5, 10 et 20 mm), les cartes du dernier run en mémoire ; 404 hors de la métropole et de la Corse, 503 sans run récent des deux |
| `GET /v1/radar/frames` | Images de la mosaïque radar Météo-France (lame d'eau, métropole, une toutes les 5 min, 3 h gardées) : dates, zone couverte, modèle de chemin des tuiles |
| `GET /v1/radar/tiles/{epoch}/{z}/{x}/{y}.png` | Tuile PNG 256 px (Web Mercator, zooms 3 à 10) d'une image radar, classes d'intensité en couleurs, transparente sans pluie ; l'adresse porte la date de l'image : réponse immuable |
| `GET /v1/health` | État de la base SQLite et des circuits breakers |

### Radar (lame d'eau)

`DPRadar/v1` (mosaïque `METROPOLE`, observation `LAME_D_EAU`, maille 500 m, HDF5) ne rend que la dernière image : une tâche de fond la lit
dès qu'une nouvelle est publiée (date lue dans un petit document JSON avant le fichier de 2 Mo), la range sur disque (`radar/` à côté
de la base, ~230 Ko par image, `METEO_RADAR_KEEP_MINUTES`, 180 par défaut) et les tuiles sont calculées à la demande
(`app/radar_grid.py` : projection stéréographique polaire écrite en numpy, pas de pyproj). `METEO_RADAR_ENABLED=0` coupe la tâche.
Source : Météo-France, Licence Ouverte 2.0 (jeu « Package Radar » de data.gouv.fr) ; crédit dans `/v1/radar/frames`.

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
