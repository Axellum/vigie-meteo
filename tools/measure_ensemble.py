"""Compare PE-AROME's probabilities of rain with what the ground stations of Météo-France measured (DPPaquetObs, hourly).

Usage (from the repository root, with the key in the environment variable named below, never on the command line)::

    python tools/measure_ensemble.py --day 2026-11-18 --ends 3-9 --out measure_ensemble.json

For each window of three hours ending at T (UTC, T = every 3 hours of the day), the PE-AROME run of two lead times is read at the step that ends at
T: the latest run that left at least 3 hours before T ("short lead", 3 to 8 hours) and the one 12 hours older ("long lead", 15 to 20 hours). One WCS
request per product, a map of France reduced like the gateway does it (blocks of 2 x 2 nodes, mean). At every station that measured it, the forecast of
the block that holds the station is compared with the rain the station measured over the same three hours (its hourly `rr1` of T-2 h, T-1 h and T, summed).

What it measures: the Brier score and its skill against the climatology of the sample, and the reliability (for the forecasts of a bin, the share of
the events that happened). What it does not: places away from a station, the 24-hour windows of PE-ARPEGE (no archive of its members beyond four
days), snow, gusts, hail and storm (no ground measurement used), a single day (the report says which).
Quota: 2 maps per window and lead + 3 observation requests per window, spaced by `--pause` seconds (50 a minute allowed for the key).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.ensemble import AROME_POOL, AROME_PRODUCTS, BBOX, run_stamp  # noqa: E402
from app.collectors.grib2 import decode_latlon_simple  # noqa: E402
from app.ensemble_grid import GridGeometry, pool_mean  # noqa: E402

KEY_ENV = "VIGIE_METEOFRANCE_API_KEY"
WCS = "https://public-api.meteofrance.fr/public/pearome/1.0/wcs/MF-NWP-HIGHRES-PEAROME-0025-FRANCE-WCS/GetCoverage"
OBS = "https://public-api.meteofrance.fr/public/DPPaquetObs/v2/paquet/stations/horaire"
THRESHOLDS = {"rain_1mm": 1.0, "rain_5mm": 5.0}
BINS = ((0, 5), (5, 20), (20, 50), (50, 80), (80, 101))


def forecast_map(client: httpx.Client, headers: dict[str, str], key: str, run: datetime, hour: int, pause: float) -> tuple[GridGeometry, np.ndarray]:
    product = next(p for p in AROME_PRODUCTS if p.key == key)
    params = [
        ("service", "WCS"),
        ("version", "2.0.1"),
        ("coverageid", product.coverage.format(run=run_stamp(run))),
        ("format", "application/wmo-grib"),
        ("subset", f"time({3600 * hour})"),
        *[("subset", s) for s in BBOX],
    ]
    for _attempt in range(8):
        time.sleep(pause)
        resp = client.get(WCS + "", params=params, headers=headers, timeout=60)
        if resp.status_code == 400 and "Lat and long parameters" in resp.text:
            continue
        resp.raise_for_status()
        grid = decode_latlon_simple(resp.content)
        return GridGeometry.of(grid, AROME_POOL), np.clip(pool_mean(grid.values, AROME_POOL), 0, 100)
    raise RuntimeError("the intermittent 400 did not go away")


def observations(client: httpx.Client, headers: dict[str, str], when: datetime, pause: float) -> list[dict]:
    time.sleep(pause)
    resp = client.get(OBS, params={"date": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "format": "json"}, headers=headers, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def brier(pairs: list[tuple[float, float]]) -> dict[str, object]:
    """`pairs` = (forecast in 0..1, event 0 or 1)."""
    if not pairs:
        return {"n": 0}
    p = np.array([f for f, _ in pairs])
    o = np.array([e for _, e in pairs])
    bs = float(np.mean((p - o) ** 2))
    base = float(np.mean(o))
    reference = float(np.mean((base - o) ** 2))  # always forecasting the sample's own frequency
    table = []
    for low, high in BINS:
        mask = (p * 100 >= low) & (p * 100 < high)
        table.append(
            {
                "bin_pct": f"{low}-{min(high, 100)}",
                "n": int(mask.sum()),
                "mean_forecast_pct": round(float(p[mask].mean() * 100), 1) if mask.any() else None,
                "observed_pct": round(float(o[mask].mean() * 100), 1) if mask.any() else None,
            }
        )
    return {
        "n": len(pairs),
        "event_rate_pct": round(base * 100, 1),
        "brier": round(bs, 4),
        "brier_of_sample_frequency": round(reference, 4),
        "brier_skill": round(1 - bs / reference, 3) if reference > 0 else None,
        "reliability": table,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", required=True, help="UTC day, YYYY-MM-DD, of the windows' ends")
    parser.add_argument("--ends", required=True, help="UTC hours `a-b` of the windows' ends (multiples of 3 are used)")
    parser.add_argument("--pause", type=float, default=1.4, help="seconds between two requests")
    parser.add_argument("--out", default="measure_ensemble.json")
    parser.add_argument("--shift", type=int, default=0, help="hours added to the forecast step read (-3 reads the window that STARTS at the end)")
    args = parser.parse_args()
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"{KEY_ENV} is not set", file=sys.stderr)
        return 2
    first, last = (int(x) for x in args.ends.split("-"))
    day = datetime.fromisoformat(args.day).replace(tzinfo=UTC)
    headers = {"apikey": key, "accept": "*/*"}
    pairs: dict[tuple[str, str], list[tuple[float, float]]] = {(k, g): [] for k in THRESHOLDS for g in ("short", "long")}
    with httpx.Client() as client:
        for hour in range(first, last + 1):
            if hour % 3:
                continue
            end = day + timedelta(hours=hour)
            stations: dict[str, dict] = {}
            for back in (2, 1, 0):
                for row in observations(client, headers, end - timedelta(hours=back), args.pause):
                    sid = str(row.get("geo_id_insee"))
                    entry = stations.setdefault(sid, {"lat": row.get("lat"), "lon": row.get("lon"), "rr": []})
                    if isinstance(row.get("rr1"), (int, float)):
                        entry["rr"].append(float(row["rr1"]))
            stations = {s: e for s, e in stations.items() if len(e["rr"]) == 3 and isinstance(e["lat"], (int, float)) and isinstance(e["lon"], (int, float))}
            # the latest PE-AROME run (03, 09, 15, 21 h) that left at least 3 hours before the end, then the one 12 hours older
            latest = end - timedelta(hours=3)
            while (latest.hour - 3) % 6:
                latest -= timedelta(hours=1)
            for group, run in (("short", latest), ("long", latest - timedelta(hours=12))):
                lead = int((end - run).total_seconds() // 3600)
                if lead > 51:
                    continue
                for name, threshold in THRESHOLDS.items():
                    if lead + args.shift < 1:
                        continue
                    try:
                        geometry, grid = forecast_map(client, headers, name, run, lead + args.shift, args.pause)
                    except (httpx.HTTPError, RuntimeError) as exc:
                        print(f"run {run:%d %H}Z +{lead} h {name}: {exc}", file=sys.stderr)
                        continue
                    for entry in stations.values():
                        cell = geometry.cell(entry["lat"], entry["lon"])
                        if cell is None:
                            continue
                        pairs[(name, group)].append((float(grid[cell]) / 100.0, 1.0 if sum(entry["rr"]) >= threshold else 0.0))
            print(f"{end:%d %H}Z done: {len(stations)} stations", file=sys.stderr)
    report: dict[str, object] = {"day": args.day, "ends": args.ends, "block_nodes": AROME_POOL, "stations_measure": "rr1 summed over 3 hours", "shift_hours": args.shift}
    for (name, group), values in pairs.items():
        report[f"{name}_{group}_lead"] = brier(values)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
