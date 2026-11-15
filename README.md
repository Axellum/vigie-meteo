# Météo-France Unified API

API météo résiliente et standardisée pour la France, conçue pour tourner sur **Oracle Cloud Always Free** (2 OCPU ARM64 / 12 Go RAM).

Elle remplace et fiabilise l'API officielle Météo-France en combinant :
1. **Open-Meteo auto-hébergé (France uniquement)** : modèles **AROME HD** (~1,5 km, 0–48 h) et **ARPEGE Europe** (~11 km, J+2 à J+4) fusionnés en une seule série horaire continue.
2. **Météo-France officielle (Portail API)** : uniquement pour les données exclusives (**Vigilances départementales** et **Pluie dans l'heure**), protégées par un cache persistant **SQLite WAL** (*Stale-While-Revalidate*) et un **Circuit Breaker**.

## Standards et normes appliqués
- **OpenAPI 3.1** (`/docs`, `/redoc`, `/openapi.json`)
- **RFC 9457** (`application/problem+json` pour toutes les erreurs HTTP)
- **RFC 7946** (export GeoJSON sur `/v1/vigilances/geojson`)
- **WMO 4677** (codes météo normalisés) & **ISO-8601 UTC**
- **Aucune donnée inventée** : une information absente ou indisponible vaut `null`.

## Endpoints principaux

| Route | Usage |
| :--- | :--- |
| `GET /v1/weather?lat=48.85&lon=2.35&dept=75` | Endpoint unifié complet (actuel, pluie 1h, vigilance, horaire fusionné, journalier) |
| `GET /v1/compact?lat=48.85&lon=2.35&dept=75` | Payload condensé (< 4 Ko) pour ESP32 / LVGL / écrans domotiques |
| `GET /v1/vigilances?dept=75` | Vigilances officielles Météo-France (toute la France ou par département) |
| `GET /v1/vigilances/geojson` | Vigilances au format GeoJSON (RFC 7946) |
| `GET /v1/rain-nowcast?lat=48.85&lon=2.35` | Pluie dans l'heure (tranches 5–10 min) |
| `GET /v1/forecast?...` | Proxy 100 % compatible avec la signature Open-Meteo |
| `GET /v1/health` | État de la base SQLite et des circuits breakers |

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
