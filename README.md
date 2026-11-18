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
| `GET /v1/radar/frames` | Images de la mosaïque radar Météo-France (lame d'eau, métropole, une toutes les 5 min, 3 h gardées) : dates, zone couverte, modèle de chemin des tuiles |
| `GET /v1/radar/tiles/{epoch}/{z}/{x}/{y}.png` | Tuile PNG 256 px (Web Mercator, zooms 3 à 10) d'une image radar, classes d'intensité en couleurs, transparente sans pluie ; l'adresse porte la date de l'image : réponse immuable |
| `GET /v1/health` | État de la base SQLite et des circuits breakers |

### Radar (lame d'eau)

`DPRadar/v1` (mosaïque `METROPOLE`, observation `LAME_D_EAU`, maille 500 m, HDF5) ne rend que la dernière image : une tâche de fond la lit
dès qu'une nouvelle est publiée (date lue dans un petit document JSON avant le fichier de 2 Mo), la range sur disque (`radar/` à côté
de la base, ~230 Ko par image, `METEO_RADAR_KEEP_MINUTES`, 180 par défaut) et les tuiles sont calculées à la demande
(`app/radar_grid.py` : projection stéréographique polaire écrite en numpy, pas de pyproj). `METEO_RADAR_ENABLED=0` coupe la tâche.
Source : Météo-France, Licence Ouverte 2.0 (jeu « Package Radar » de data.gouv.fr) ; crédit dans `/v1/radar/frames`.

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
