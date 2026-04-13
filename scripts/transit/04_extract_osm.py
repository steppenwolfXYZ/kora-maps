#!/usr/bin/env python3
"""
Extract from the Switzerland OSM PBF:
  1. Transit route relations → data/osm/routes.geojson
  2. Transit stations/stops  → data/osm/stations.geojson

Run after 03_download_osm.py.

Route geometry is stitched from ordered member ways.
Stations include parent stations (location_type=1 equivalent) with
name, operator, uic_ref (Swiss Didok code), and all relevant tags.
"""

import json
import osmium
from math import radians, cos, sin, sqrt, atan2
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[2]
PBF = ROOT / "data" / "osm" / "switzerland-latest.osm.pbf"
OUT_ROUTES = ROOT / "data" / "osm" / "routes.geojson"
OUT_STATIONS = ROOT / "data" / "osm" / "stations.geojson"

# Landuse values considered "urban" for bus route classification.
# Routes where >50% of sampled points fall in urban cells → city bus, else regional_bus.
URBAN_LANDUSE = {
    "residential", "commercial", "industrial", "retail",
    "construction", "garages", "depot", "brownfield",
}
# Grid cell size in degrees (~180m at Swiss latitudes).
URBAN_GRID_DEG = 0.002

# Route types we care about
TRANSIT_ROUTES = {
    "train", "railway", "rail",
    "subway", "light_rail",
    "tram", "trolleybus",
    "bus", "coach",
    "ferry", "boat",
    "gondola", "aerial_lift", "cable_car",
    "funicular",
    "aerialway",   # cable cars / gondolas tagged as route=aerialway
}

# OSM tags that identify transit stops/stations
STATION_TAGS = {
    "railway": {"station", "halt", "tram_stop", "platform"},
    "public_transport": {"station", "stop_position", "platform", "stop_area"},
    "amenity": {"bus_station"},
    "aerialway": {"station"},
}


def haversine_km(lon1, lat1, lon2, lat2) -> float:
    R = 6371
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi, dlam = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlam / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


# ---------------------------------------------------------------------------
# Pass 1: collect all node coordinates and way geometries
# ---------------------------------------------------------------------------

class GeometryCollector(osmium.SimpleHandler):
    """Collect node coords and way node-lists needed to build route geometries."""

    def __init__(self):
        super().__init__()
        self.node_coords: dict[int, tuple[float, float]] = {}  # id → (lon, lat)
        self.way_nodes: dict[int, list[int]] = {}              # id → [node_ids]
        self.urban_way_ids: set = set()                        # urban landuse ways

    def node(self, n):
        if n.location.valid():
            self.node_coords[n.id] = (n.location.lon, n.location.lat)

    def way(self, w):
        self.way_nodes[w.id] = [n.ref for n in w.nodes]
        if dict(w.tags).get("landuse") in URBAN_LANDUSE:
            self.urban_way_ids.add(w.id)

    def relation(self, r):
        # Large urban landuse areas are usually OSM multipolygon relations whose
        # member ways don't carry the landuse tag themselves (only the relation does).
        # Mark all outer member ways of such relations as urban too.
        if dict(r.tags).get("landuse") in URBAN_LANDUSE:
            for m in r.members:
                if m.type == "w":
                    self.urban_way_ids.add(m.ref)


def build_urban_grid(urban_way_ids: set, way_nodes: dict, node_coords: dict) -> set:
    """Build a set of (ix, iy) grid cells covered by urban landuse polygons.
    Uses bounding-box fill at URBAN_GRID_DEG resolution (~180m).
    Skips polygons whose bbox exceeds 0.2° per side (>22km — not a real urban block).
    """
    urban_cells: set = set()
    for wid in urban_way_ids:
        nodes = way_nodes.get(wid, [])
        coords = [node_coords[n] for n in nodes if n in node_coords]
        if len(coords) < 3:
            continue
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        min_lon, max_lon = min(lons), max(lons)
        min_lat, max_lat = min(lats), max(lats)
        if max_lon - min_lon > 0.2 or max_lat - min_lat > 0.2:
            continue  # skip huge polygons (forests, entire districts)
        ix0 = int(min_lon / URBAN_GRID_DEG)
        ix1 = int(max_lon / URBAN_GRID_DEG)
        iy0 = int(min_lat / URBAN_GRID_DEG)
        iy1 = int(max_lat / URBAN_GRID_DEG)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                urban_cells.add((ix, iy))
    return urban_cells


def route_urban_fraction(chunks: list, urban_cells: set, sample_km: float = 0.25) -> float:
    """Sample points along the route every sample_km and return the fraction
    that fall in an urban grid cell.  chunks is a list of coordinate lists."""
    total = 0
    urban = 0
    for coords in chunks:
        for i in range(len(coords) - 1):
            lon1, lat1 = coords[i][0], coords[i][1]
            lon2, lat2 = coords[i + 1][0], coords[i + 1][1]
            seg_km = haversine_km(lon1, lat1, lon2, lat2)
            n_samples = max(1, int(seg_km / sample_km))
            for j in range(n_samples):
                t = j / n_samples
                slon = lon1 + t * (lon2 - lon1)
                slat = lat1 + t * (lat2 - lat1)
                ix = int(slon / URBAN_GRID_DEG)
                iy = int(slat / URBAN_GRID_DEG)
                total += 1
                if (ix, iy) in urban_cells:
                    urban += 1
    return urban / total if total > 0 else 0.5


# ---------------------------------------------------------------------------
# Pass 2: extract route relations and station nodes/ways
# ---------------------------------------------------------------------------

class TransitExtractor(osmium.SimpleHandler):

    def __init__(self, node_coords, way_nodes, urban_cells: set):
        super().__init__()
        self.node_coords = node_coords
        self.way_nodes = way_nodes
        self.urban_cells = urban_cells
        self.route_features: list[dict] = []
        self.station_features: list[dict] = []

    # -- Helpers -------------------------------------------------------------

    def _way_coords(self, way_id: int) -> list[list[float]]:
        nodes = self.way_nodes.get(way_id, [])
        coords = []
        for nid in nodes:
            c = self.node_coords.get(nid)
            if c:
                coords.append(list(c))
        return coords

    # Maximum junction gap (km) between the endpoint of one way and the startpoint
    # of the next way in the OSM relation.  Adjacent ways in a well-mapped relation
    # share an OSM node, so the gap is essentially 0.  We allow 100 m of tolerance
    # for minor OSM imprecision.  Gaps larger than this mean the two ways are not
    # actually connected (different branch, missing section, cross-border gap) and
    # should become separate segments.
    #
    # This threshold applies only at way boundaries — we never split inside a way.
    # A single OSM way is always a valid continuous line regardless of node spacing.
    MAX_JUNCTION_GAP_KM = 0.1   # 100 m, uniform for all route types

    def _stitch_ways(self, way_ids: list[int], route_type: str = "default") -> list[list[list[float]]]:
        """Stitch consecutive ways into continuous segments.

        Two ways are merged only if their junction gap (haversine distance from the
        end of the current segment to the nearest endpoint of the next way) is within
        MAX_JUNCTION_GAP_KM.  Otherwise a new segment is started.

        Coordinates inside a single way are never split — a way is always valid.

        Returns a list of coordinate lists (one per continuous segment).
        Segments shorter than 50 m are discarded.
        """
        segs = [self._way_coords(wid) for wid in way_ids]
        segs = [s for s in segs if len(s) >= 2]
        if not segs:
            return []

        chunks: list[list[list[float]]] = [list(segs[0])]

        for seg in segs[1:]:
            end = chunks[-1][-1]
            s_start, s_end = seg[0], seg[-1]
            d_fwd = haversine_km(*end, *s_start)
            d_rev = haversine_km(*end, *s_end)

            if d_fwd <= d_rev:
                junction_gap, coords_to_add = d_fwd, seg[1:]
            else:
                junction_gap, coords_to_add = d_rev, list(reversed(seg[:-1]))

            if junction_gap <= self.MAX_JUNCTION_GAP_KM:
                chunks[-1].extend(coords_to_add)
            else:
                # Ways are not adjacent — keep as a separate segment
                chunks.append(list(seg) if d_fwd <= d_rev else list(reversed(seg)))

        # Drop tiny fragments (< 50 m)
        return [c for c in chunks if self._route_length_km(c) >= 0.05]

    def _route_length_km(self, coords: list) -> float:
        if len(coords) < 2:
            return 0.0
        return sum(
            haversine_km(*coords[i], *coords[i + 1])
            for i in range(len(coords) - 1)
        )

    def _is_transit_station(self, tags) -> bool:
        for key, values in STATION_TAGS.items():
            v = tags.get(key)
            if v and v in values:
                return True
        return False

    # -- Handlers ------------------------------------------------------------

    def node(self, n):
        tags = dict(n.tags)
        if not self._is_transit_station(tags):
            return
        if not n.location.valid():
            return
        name = tags.get("name") or tags.get("uic_name") or tags.get("ref:name", "")
        self.station_features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [n.location.lon, n.location.lat],
            },
            "properties": {
                "osm_id": f"node/{n.id}",
                "name": name,
                "uic_ref": tags.get("uic_ref", ""),
                "railway": tags.get("railway", ""),
                "public_transport": tags.get("public_transport", ""),
                "operator": tags.get("operator", ""),
                "network": tags.get("network", ""),
                "layer": tags.get("layer", ""),
            },
        })

    def way(self, w):
        tags = dict(w.tags)
        if not self._is_transit_station(tags):
            return
        # Station ways (e.g. large station buildings) — use centroid of nodes
        nodes = [self.node_coords.get(n.ref) for n in w.nodes if n.ref in self.node_coords]
        nodes = [c for c in nodes if c]
        if not nodes:
            return
        lon = sum(c[0] for c in nodes) / len(nodes)
        lat = sum(c[1] for c in nodes) / len(nodes)
        name = tags.get("name") or tags.get("uic_name", "")
        self.station_features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "osm_id": f"way/{w.id}",
                "name": name,
                "uic_ref": tags.get("uic_ref", ""),
                "railway": tags.get("railway", ""),
                "public_transport": tags.get("public_transport", ""),
                "operator": tags.get("operator", ""),
                "network": tags.get("network", ""),
                "layer": "",
            },
        })

    def relation(self, r):
        tags = dict(r.tags)

        route = tags.get("route", "")
        rel_type = tags.get("type", "")

        # Route relations
        if rel_type == "route" and route in TRANSIT_ROUTES:
            way_ids = [
                m.ref for m in r.members
                if m.type == "w"
                and m.role not in ("platform", "platform_entry_only",
                                   "platform_exit_only", "stop",
                                   "stop_entry_only", "stop_exit_only")
            ]
            # Also compute total way length before gap-splitting, for regional_bus classification.
            # gap-split chunks may be shorter if OSM ways have gaps, so we need the raw total.
            all_way_coords = [self._way_coords(wid) for wid in way_ids]
            raw_total_km = sum(self._route_length_km(c) for c in all_way_coords if len(c) >= 2)

            chunks = self._stitch_ways(way_ids, route)
            if not chunks:
                return
            total_length_km = sum(self._route_length_km(c) for c in chunks)
            if total_length_km < 0.05:
                return

            # Emit MultiLineString when multiple disjoint segments exist,
            # LineString for a single continuous segment.
            if len(chunks) == 1:
                geometry = {"type": "LineString", "coordinates": chunks[0]}
            else:
                geometry = {"type": "MultiLineString", "coordinates": chunks}

            # Compute urban fraction for bus/trolleybus routes (city vs regional split)
            urban_frac = None
            if route in ("bus", "coach", "trolleybus") and self.urban_cells:
                urban_frac = round(route_urban_fraction(chunks, self.urban_cells), 3)

            self.route_features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "osm_id": r.id,
                    "route": route,
                    "ref": tags.get("ref", ""),
                    "name": tags.get("name", ""),
                    "operator": tags.get("operator", ""),
                    "network": tags.get("network", ""),
                    "colour": tags.get("colour", ""),
                    "from": tags.get("from", ""),
                    "to": tags.get("to", ""),
                    "length_km": round(total_length_km, 2),
                    "raw_length_km": round(raw_total_km, 2),
                    "way_count": len(way_ids),
                    "urban_fraction": urban_frac,
                },
            })

        # Stop areas (relation grouping platforms + stops for one station)
        elif rel_type == "public_transport" and tags.get("public_transport") == "stop_area":
            # Collect centroid from member nodes
            coords_list = []
            for m in r.members:
                if m.type == "n":
                    c = self.node_coords.get(m.ref)
                    if c:
                        coords_list.append(c)
            if not coords_list:
                return
            lon = sum(c[0] for c in coords_list) / len(coords_list)
            lat = sum(c[1] for c in coords_list) / len(coords_list)
            name = tags.get("name") or tags.get("uic_name", "")
            self.station_features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "osm_id": f"relation/{r.id}",
                    "name": name,
                    "uic_ref": tags.get("uic_ref", ""),
                    "railway": tags.get("railway", ""),
                    "public_transport": "stop_area",
                    "operator": tags.get("operator", ""),
                    "network": tags.get("network", ""),
                    "layer": "",
                },
            })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Pass 1: collecting node coordinates and way geometries...")
    geo = GeometryCollector()
    geo.apply_file(str(PBF), locations=True)
    print(f"  {len(geo.node_coords):,} nodes, {len(geo.way_nodes):,} ways collected")

    print(f"  Building urban grid from {len(geo.urban_way_ids):,} landuse polygons...")
    urban_cells = build_urban_grid(geo.urban_way_ids, geo.way_nodes, geo.node_coords)
    print(f"  → {len(urban_cells):,} urban grid cells at {URBAN_GRID_DEG}° resolution")

    print("Pass 2: extracting transit routes and stations...")
    extractor = TransitExtractor(geo.node_coords, geo.way_nodes, urban_cells)
    extractor.apply_file(str(PBF))

    routes = extractor.route_features
    stations = extractor.station_features

    # Write routes
    OUT_ROUTES.parent.mkdir(parents=True, exist_ok=True)
    OUT_ROUTES.write_text(json.dumps({"type": "FeatureCollection", "features": routes}, indent=2))

    # Write stations
    OUT_STATIONS.write_text(json.dumps({"type": "FeatureCollection", "features": stations}, indent=2))

    # Summary
    print(f"\nRoutes: {len(routes):,} features → {OUT_ROUTES}")
    route_types = {}
    for f in routes:
        rt = f["properties"]["route"]
        route_types[rt] = route_types.get(rt, 0) + 1
    for rt, count in sorted(route_types.items(), key=lambda x: -x[1]):
        print(f"  {rt:<20} {count:>6,}")

    print(f"\nStations: {len(stations):,} features → {OUT_STATIONS}")
    pt_types = {}
    for f in stations:
        key = f["properties"].get("public_transport") or f["properties"].get("railway") or "other"
        pt_types[key] = pt_types.get(key, 0) + 1
    for pt, count in sorted(pt_types.items(), key=lambda x: -x[1]):
        print(f"  {pt:<25} {count:>6,}")


if __name__ == "__main__":
    main()
