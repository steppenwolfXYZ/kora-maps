#!/usr/bin/env python3
"""Run the bicycle routing benchmark set against a Valhalla instance.

bicycle-costing-fork.md § Benchmark set: every tuning iteration of the
forked costing runs every pair in valhalla/fork/bicycle_benchmark.yaml and
a change ships only when no pair regresses. This script is that check.

For each pair it requests the primary route (plus alternates, printed for
context) with the app's default bicycle options and prints the street chain,
then judges the primary against the pair's must_include / must_exclude
street names. Exit status is the number of failing pairs, so it doubles as
a gate.

    python3 scripts/bicycle_benchmark.py                 # all pairs, localhost:8002
    python3 scripts/bicycle_benchmark.py --only bern-eichmatt-viktoria
    python3 scripts/bicycle_benchmark.py --url http://localhost:8002 --exclude-steps
    python3 scripts/bicycle_benchmark.py --json out.json  # also dump the raw responses

Requires a running Valhalla (the fork or stock — pass --url to point at a
different port when comparing the two side by side). PyYAML only.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BENCHMARK = ROOT / "valhalla" / "fork" / "bicycle_benchmark.yaml"

# Mirror the client's request (src/lib/routing/valhalla.ts): hybrid bike,
# strong hill avoidance, maneuvers for the street chain.
DEFAULT_OPTIONS = {"bicycle_type": "hybrid", "use_hills": 0.1}


def request(url: str, pair: dict, options: dict, alternates: int) -> dict:
    body = {
        "costing": "bicycle",
        "costing_options": {"bicycle": options},
        "locations": [
            {"lat": pair["from"][1], "lon": pair["from"][0]},
            {"lat": pair["to"][1], "lon": pair["to"][0]},
        ],
        "alternates": alternates,
        "units": "kilometers",
        "directions_type": "maneuvers",
    }
    req = urllib.request.Request(
        f"{url.rstrip('/')}/route",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def street_chain(trip: dict) -> list[str]:
    names: list[str] = []
    for leg in trip.get("legs", []):
        for m in leg.get("maneuvers", []):
            sn = m.get("street_names") or []
            if sn and (not names or names[-1] != sn[0]):
                names.append(sn[0])
    return names


def summarize(trip: dict) -> str:
    s = trip["summary"]
    return f"{s['length']:.2f} km  {round(s['time'] / 60):>3d} min   " + " › ".join(street_chain(trip))


def judge(chain: list[str], pair: dict) -> list[str]:
    joined = " | ".join(chain).lower()
    problems = []
    for name in pair.get("must_include", []) or []:
        if name.lower() not in joined:
            problems.append(f"missing {name!r}")
    for name in pair.get("must_exclude", []) or []:
        if name.lower() in joined:
            problems.append(f"touches {name!r}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8002", help="Valhalla base URL")
    ap.add_argument("--only", action="append", help="pair id to run (repeatable)")
    ap.add_argument("--alternates", type=int, default=2, help="alternates to print for context")
    ap.add_argument("--exclude-steps", action="store_true", help="send exclude_steps=true (the avoid-stairs toggle)")
    ap.add_argument("--json", type=Path, help="dump raw responses to this file")
    args = ap.parse_args()

    pairs = yaml.safe_load(BENCHMARK.read_text())["pairs"]
    if args.only:
        pairs = [p for p in pairs if p["id"] in set(args.only)]
        if not pairs:
            print(f"no pair matches {args.only}", file=sys.stderr)
            return 2

    options = dict(DEFAULT_OPTIONS)
    if args.exclude_steps:
        options["exclude_steps"] = True

    failures = 0
    raw: dict[str, dict] = {}
    for pair in pairs:
        print(f"== {pair['id']}")
        try:
            resp = request(args.url, pair, options, args.alternates)
        except (urllib.error.URLError, OSError) as e:
            print(f"   request failed: {e}")
            failures += 1
            continue
        raw[pair["id"]] = resp
        trips = [resp["trip"]] + [a["trip"] for a in resp.get("alternates", []) if a.get("trip")]
        for i, trip in enumerate(trips):
            tag = "primary " if i == 0 else f"alt {i}    "
            print(f"   {tag}{summarize(trip)}")
        problems = judge(street_chain(trips[0]), pair)
        if problems:
            failures += 1
            print(f"   FAIL  {'; '.join(problems)}")
        else:
            print("   ok")
    if args.json:
        args.json.write_text(json.dumps(raw, indent=1, ensure_ascii=False))
    print(f"\n{len(pairs) - failures}/{len(pairs)} pairs pass")
    return failures


if __name__ == "__main__":
    sys.exit(main())
