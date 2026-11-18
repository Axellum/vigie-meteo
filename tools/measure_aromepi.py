"""Compare AROME-PI's gusts, visibility and rain with what the ground stations of Météo-France measured (DPPaquetObs, 6 minutes).

Usage (from the repository root, with the key in the environment variable named below, never on the command line)::

    python tools/measure_aromepi.py --day 2026-11-18 --hours 2-8 --out measure.json

For each hour T of the day and each lead time (1, 3 and 6 hours), the run issued `lead` hours before T is read at the step that ends at T
(one WCS request per product, a map of France reduced like the gateway does it) and compared, at every station that measured it, with
what the station measured over the same 15 minutes: the strongest `raf10` of its observations at T and T-6 min (a gust of 10 minutes
each), the lowest visibility `vv`, the rain `rr_per` summed over the observations at T, T-6 and T-12 min. The forecast value of a station
is the one the gateway would serve for its position (the worst of a block of 4 x 4 nodes of 0.01 degree).

What it measures: bias and RMSE of the gusts, and for a threshold the hits, misses and false alarms (a "hit" is both forecast and observed
at or above the threshold in the same 15 minutes). What it does not: hail (no station measures it), the type of precipitation other than
the presence of rain, the places away from a station, a single day (the report says which).
Quota: 3 products x 3 leads x hours + 2 observation requests per hour, spaced by `--pause` seconds (50 a minute allowed for the key).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.aromepi_grid import POOL, Geometry, pool_code, pool_max, pool_min  # noqa: E402
from app.collectors.aromepi import BBOX, PRODUCTS, run_stamp  # noqa: E402
from app.collectors.grib2 import decode_latlon_simple  # noqa: E402

KEY_ENV = "VIGIE_METEOFRANCE_API_KEY"
WCS = "https://public-api.meteofrance.fr/public/aromepi/1.0/wcs/MF-NWP-HIGHRES-AROMEPI-001-FRANCE-WCS/GetCoverage"
OBS = "https://public-api.meteofrance.fr/public/DPPaquetObs/v2/paquet/stations/infrahoraire-6m"
LEADS = (1, 3, 6)
GUST_THRESHOLDS_KMH = (60, 90)
VISIBILITY_THRESHOLD_M = 1000
RAIN_THRESHOLD_MM = 0.2


def pace(pause: float) -> None:
    time.sleep(pause)


def forecast_map(client: httpx.Client, headers: dict[str, str], key: str, run: datetime, step: int, pause: float) -> tuple[Geometry, np.ndarray]:
    product = next(p for p in PRODUCTS if p.key == key)
    params = [
        ("service", "WCS"),
        ("version", "2.0.1"),
        ("coverageid", product.coverage.format(run=run_stamp(run))),
        ("format", "application/wmo-grib"),
        ("subset", f"time({900 * step})"),
        *[("subset", s) for s in product.extra_subsets],
        *[("subset", s) for s in BBOX],
    ]
    pace(pause)
    resp = client.get(WCS, params=params, headers=headers, timeout=60)
    resp.raise_for_status()
    grid = decode_latlon_simple(resp.content)
    values = grid.values
    reduced = {"gust": lambda v: pool_max(v) * 3.6, "visibility": pool_min, "ptype": pool_code}[key](values)
    return Geometry.of(grid), reduced


def observations(client: httpx.Client, headers: dict[str, str], when: datetime, pause: float) -> list[dict]:
    pace(pause)
    resp = client.get(OBS, params={"date": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "format": "json"}, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def at(reduced: np.ndarray, geometry: Geometry, lat: float, lon: float) -> float | None:
    cell = geometry.cell(lat, lon)
    return None if cell is None else float(reduced[cell])


def contingency(pairs: list[tuple[float, float]], threshold: float, below: bool = False) -> dict[str, float | int | None]:
    """Hits, misses and false alarms of `forecast >= threshold` (`<` when `below`) against `observed`, over (forecast, observed) pairs."""
    hit = miss = false = quiet = 0
    for forecast, observed in pairs:
        f = forecast < threshold if below else forecast >= threshold
        o = observed < threshold if below else observed >= threshold
        hit += f and o
        miss += (not f) and o
        false += f and not o
        quiet += (not f) and not o
    pod = hit / (hit + miss) if hit + miss else None
    far = false / (hit + false) if hit + false else None
    return {"n": len(pairs), "hits": hit, "misses": miss, "false_alarms": false, "correct_quiet": quiet, "pod": pod, "far": far}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", required=True, help="UTC day, YYYY-MM-DD")
    parser.add_argument("--hours", required=True, help="UTC hours of the checks, `a-b` (the hour T of each comparison)")
    parser.add_argument("--pause", type=float, default=2.5, help="seconds between two requests")
    parser.add_argument("--out", default="measure_aromepi.json")
    args = parser.parse_args()
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"{KEY_ENV} is not set", file=sys.stderr)
        return 2
    first, last = (int(x) for x in args.hours.split("-"))
    day = datetime.fromisoformat(args.day).replace(tzinfo=UTC)
    headers = {"apikey": key, "accept": "*/*"}
    gust: dict[int, list[tuple[float, float]]] = {lead: [] for lead in LEADS}
    vis: dict[int, list[tuple[float, float]]] = {lead: [] for lead in LEADS}
    rain: dict[int, list[tuple[float, float]]] = {lead: [] for lead in LEADS}
    with httpx.Client() as client:
        for hour in range(first, last + 1):
            t = day + timedelta(hours=hour)
            obs_by_time = {t - timedelta(minutes=m): observations(client, headers, t - timedelta(minutes=m), args.pause) for m in (0, 6, 12)}
            stations: dict[str, dict] = {}
            for when, rows in obs_by_time.items():
                for row in rows:
                    sid = str(row.get("geo_id_insee"))
                    entry = stations.setdefault(sid, {"lat": row.get("lat"), "lon": row.get("lon"), "raf": [], "vv": [], "rr": [], "rr_n": 0})
                    in_gust_window = when >= t - timedelta(minutes=6)
                    if in_gust_window and isinstance(row.get("raf10"), (int, float)):
                        entry["raf"].append(float(row["raf10"]) * 3.6)
                    if in_gust_window and isinstance(row.get("vv"), (int, float)):
                        entry["vv"].append(float(row["vv"]))
                    if isinstance(row.get("rr_per"), (int, float)):
                        entry["rr"].append(float(row["rr_per"]))
            for lead in LEADS:
                run = t - timedelta(hours=lead)
                step = lead * 4
                try:
                    geometry, gust_map = forecast_map(client, headers, "gust", run, step, args.pause)
                    _, vis_map = forecast_map(client, headers, "visibility", run, step, args.pause)
                    _, type_map = forecast_map(client, headers, "ptype", run, step, args.pause)
                except httpx.HTTPError as exc:
                    print(f"run {run:%d %H}Z step {step}: {exc}", file=sys.stderr)
                    continue
                for entry in stations.values():
                    if not isinstance(entry["lat"], (int, float)) or not isinstance(entry["lon"], (int, float)):
                        continue
                    if entry["raf"]:
                        f = at(gust_map, geometry, entry["lat"], entry["lon"])
                        if f is not None:
                            gust[lead].append((f, max(entry["raf"])))
                    if entry["vv"]:
                        f = at(vis_map, geometry, entry["lat"], entry["lon"])
                        if f is not None:
                            vis[lead].append((f, min(entry["vv"])))
                    if len(entry["rr"]) == 3:
                        f = at(type_map, geometry, entry["lat"], entry["lon"])
                        if f is not None:
                            rain[lead].append((1.0 if f != 0 else 0.0, sum(entry["rr"])))
            print(f"{t:%H}Z done", file=sys.stderr)

    report: dict[str, object] = {"day": args.day, "hours": args.hours, "block_nodes": POOL, "leads_h": list(LEADS)}
    for lead in LEADS:
        pairs = gust[lead]
        errors = [f - o for f, o in pairs]
        report[f"gust_lead_{lead}h"] = {
            "n": len(pairs),
            "bias_kmh": float(np.mean(errors)) if errors else None,
            "rmse_kmh": float(math.sqrt(np.mean(np.square(errors)))) if errors else None,
            "observed_mean_kmh": float(np.mean([o for _, o in pairs])) if pairs else None,
            **{f"ge_{t}": contingency(pairs, t) for t in GUST_THRESHOLDS_KMH},
        }
        report[f"visibility_lead_{lead}h"] = {"lt_1000m": contingency(vis[lead], VISIBILITY_THRESHOLD_M, below=True)}
        report[f"rain_lead_{lead}h"] = {"rain_ge_0.2mm_in_18min": contingency([(f, 1.0 if o >= RAIN_THRESHOLD_MM else 0.0) for f, o in rain[lead]], 0.5)}
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
