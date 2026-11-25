"""Replay the detector over stored Prometheus history to compare baseline lengths on real data.

    python evals/replay_detector.py --start 11:00 --end 12:48 --baselines 10 30 \
        --fault 11:05:44-11:13:44 --fault 12:01:10-12:05:25        # times: local, today

At each step it runs the detector's exact queries as of that moment (Prometheus `time=`), applies the same
2-consecutive-checks rule and cooldown, and scores every alert: inside a fault (or up to --tail-min after it) = true,
otherwise = false alarm. Reports detection delay per fault.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import detector  # noqa: E402
import telemetry  # noqa: E402


def at(day: datetime, hhmm: str) -> datetime:
    parts = [int(x) for x in hhmm.split(":")] + [0]
    return day.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)


def replay(start: datetime, end: datetime, step_s: int, baseline: int) -> list[tuple[datetime, list]]:
    detector.configure(baseline)
    now = {"t": start}
    real = telemetry._prom

    def prom_at(q: str):
        for attempt in range(3):  # a long replay outlives a Prometheus hiccup (or a laptop sleep)
            try:
                r = telemetry._request("GET", f"{telemetry.PROM_URL}/api/v1/query",
                                       params={"query": q, "time": now["t"].timestamp()})
                r.raise_for_status()
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
        rows = []
        for item in r.json()["data"]["result"]:
            try:
                v = float(item["value"][1])
            except (KeyError, ValueError, TypeError):
                continue
            if v == v and v not in (float("inf"), float("-inf")):
                rows.append({"labels": item.get("metric", {}), "value": v})
        return rows, None

    telemetry._prom = prom_at
    det = detector.Detector(clock=lambda: now["t"].timestamp())
    detector.CONSECUTIVE = max(1, round(30 / step_s))  # same 30 s hold as the live 2 x 15 s checks
    alerts = []
    try:
        while now["t"] <= end:
            ready = det.check(detector.breaches(detector.measure()))
            if ready:
                alerts.append((now["t"], ready))
            now["t"] += timedelta(seconds=step_s)
    finally:
        telemetry._prom = real
        detector.CONSECUTIVE = 2
    return alerts


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--fault", action="append", default=[], help="HH:MM[:SS]-HH:MM[:SS], local time today")
    p.add_argument("--baselines", type=int, nargs="+", default=[10, 30])
    p.add_argument("--no-warmup", action="store_true", help="disable the 10-min warm-up fallback (compare pure baselines)")
    p.add_argument("--date", help="YYYY-MM-DD of the times (default: today)")
    p.add_argument("--step-s", type=int, default=15)
    p.add_argument("--tail-min", type=float, default=4, help="errors linger this long after a fault ends")
    a = p.parse_args()
    day = datetime.strptime(a.date, "%Y-%m-%d") if a.date else datetime.now()
    start, end = at(day, a.start), at(day, a.end)
    faults = [(at(day, f.split("-")[0]), at(day, f.split("-")[1])) for f in a.fault]
    tail = timedelta(minutes=a.tail_min)
    if a.no_warmup:
        detector.WARMUP_BASELINE_MIN = 10**6
    for b in a.baselines:
        alerts = replay(start, end, a.step_s, b)
        false = [(t, r) for t, r in alerts if not any(s <= t <= e + tail for s, e in faults)]
        print(f"\n=== baseline {b} min: {len(alerts)} alert(s), {len(false)} false alarm(s)")
        for t, r in alerts:
            tag = "FALSE" if (t, r) in false else "true "
            print(f"  {t:%H:%M:%S} {tag} {'; '.join(x.text for x in r)[:150]}")
        for s, e in faults:
            hit = [t for t, _ in alerts if s <= t <= e + tail]
            delay = f"{(hit[0] - s).total_seconds():.0f} s" if hit else "MISSED"
            print(f"  fault {s:%H:%M:%S}-{e:%H:%M:%S}: detected after {delay}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
