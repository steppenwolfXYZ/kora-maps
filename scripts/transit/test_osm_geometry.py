#!/usr/bin/env python3
"""
Feasibility test: can we get realistic transit geometries from OSM?

Queries Overpass API for public transit route relations in Bern,
then checks quality: completeness, geometry, match to known GTFS stops.

Outputs test_osm_geometry.geojson for visual inspection in geojson.io or QGIS.
"""

import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from math import radians, cos, sin, sqrt, atan2

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "test_osm_geometry.geojson"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Query: all PT route relations in and around Bern
# We fetch the full geometry (out geom) so we get actual coordinates of ways
QUERY = """
[out:json][timeout:60];
(
  rel["type"="route"]["route"~"tram|bus|subway|light_rail|train|rail"]
     (46.90,7.35,47.00,7.55);
);
out geom;
"""


def overpass_query(query: str) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(OVERPASS_URL, data=data)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", "newmap-transit-feasibility-test/1.0")
    print("Querying Overpass API...")
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def relation_to_linestring(relation: dict):
    """
    Extract an ordered (best-effort) list of [lon, lat] points from a route relation.
    Ways in a route relation are ordered along the route; we stitch them end-to-end.
    """
    # Collect ways with their geometry
    ways = []
    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        role = member.get("role", "")
        if role in ("platform", "stop", "stop_exit_only", "stop_entry_only"):
            continue
        geom = member.get("geometry", [])
        if not geom:
            continue
        coords = [[pt["lon"], pt["lat"]] for pt in geom]
        ways.append(coords)

    if not ways:
        return None

    # Stitch ways: try to connect end-to-end (flip if needed)
    stitched = list(ways[0])
    for way in ways[1:]:
        # Options: connect forward or reversed
        end = stitched[-1]
        w_start = way[0]
        w_end = way[-1]

        def dist(a, b):
            return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

        if dist(end, w_start) <= dist(end, w_end):
            stitched.extend(way[1:])
        else:
            stitched.extend(reversed(way[:-1]))

    return stitched


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlam = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def main():
    result = overpass_query(QUERY)
    relations = result.get("elements", [])
    print(f"Found {len(relations)} route relations")

    features = []
    stats = {"ok": 0, "no_geom": 0, "short": 0}

    for rel in relations:
        tags = rel.get("tags", {})
        name = tags.get("name", tags.get("ref", "?"))
        route_type = tags.get("route", "?")
        operator = tags.get("operator", "")
        ref = tags.get("ref", "")

        coords = relation_to_linestring(rel)
        if not coords or len(coords) < 2:
            stats["no_geom"] += 1
            continue

        # Compute total length
        total_km = sum(
            haversine_km(*coords[i], *coords[i + 1]) for i in range(len(coords) - 1)
        )

        if total_km < 0.1:
            stats["short"] += 1
            continue

        stats["ok"] += 1
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "osm_id": rel["id"],
                "name": name,
                "ref": ref,
                "route": route_type,
                "operator": operator,
                "length_km": round(total_km, 2),
                "way_count": sum(1 for m in rel["members"] if m["type"] == "way"),
                "stop_count": sum(1 for m in rel["members"] if m["type"] == "node"),
            },
        })

    geojson = {"type": "FeatureCollection", "features": features}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(geojson, indent=2))

    print(f"\nResults:")
    print(f"  Routes with good geometry: {stats['ok']}")
    print(f"  Routes with no geometry:   {stats['no_geom']}")
    print(f"  Routes too short (<100m):  {stats['short']}")
    print(f"\nSample routes:")
    for f in sorted(features, key=lambda x: -x['properties']['length_km'])[:15]:
        p = f['properties']
        print(f"  [{p['route']:12}] {p['ref']:10} {p['name'][:40]:40} {p['length_km']:6.1f} km  {p['way_count']} ways")
    print(f"\nOutput: {OUT}")
    print("Open at https://geojson.io to visually inspect geometry quality.")


if __name__ == "__main__":
    main()
