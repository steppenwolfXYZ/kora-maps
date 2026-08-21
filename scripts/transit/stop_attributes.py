"""Step-07 stop loaders + terminus-skip computation.

Loads the pfaedle-routed GTFS stops.txt plus the pre-pfaedle SLOID column,
the atlas boarding-platform CSV, and step 06's stop-tier scores. Also
holds `compute_terminus_skip_oids` and `write_stop_attributes_diag`, both
of which sit alongside these loaders in the pipeline flow (they consume
their outputs directly).
"""
import csv
import json

from common import PROJECT_ROOT
from gtfs.stop_identity import merge_key_of
from geometry import haversine_km

GTFS_STOPS             = PROJECT_ROOT / "data" / "gtfs_routed" / "stops.txt"
GTFS_STOPS_PRE_PFAEDLE = PROJECT_ROOT / "data" / "gtfs_filtered" / "stops.txt"
ATLAS_CSV              = PROJECT_ROOT / "data" / "atlas" / "actual-date-world-traffic-point.csv"
STOP_SCORES            = PROJECT_ROOT / "data" / "transit" / "stop_size_scores.json"
OUT_STOP_ATTRS_DIAG    = PROJECT_ROOT / "data" / "transit" / "stop_attributes_sources.json"

TERMINUS_DEDUP_RADIUS_M = 10.0
ARRIVAL_DROP_MODES = {"tram", "bus", "regional_bus"}


def load_stop_sloid() -> dict:
    """Return {stop_id: sloid} from the pre-pfaedle filtered stops.txt
    (`original_stop_id` column, dropped by pfaedle in `gtfs_routed`).
    """
    out = {}
    if not GTFS_STOPS_PRE_PFAEDLE.exists():
        return out
    with open(GTFS_STOPS_PRE_PFAEDLE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            sloid = (row.get("original_stop_id") or "").strip()
            if sloid:
                out[row["stop_id"]] = sloid
    return out


def load_atlas_attributes() -> dict:
    """Return {sloid: {"length": float|None, "compass_direction": float|None}}.

    Reads only the BOARDING_PLATFORM rows from atlas v2 traffic-point CSV.
    Empty / unparseable numeric fields become None.
    """
    out = {}
    if not ATLAS_CSV.exists():
        print(f"WARNING: atlas CSV not found at {ATLAS_CSV} — attributes will be empty")
        return out

    def _f(v):
        v = (v or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    with open(ATLAS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row.get("trafficPointElementType") != "BOARDING_PLATFORM":
                continue
            sloid = row.get("sloid", "").strip()
            if not sloid:
                continue
            out[sloid] = {
                "length": _f(row.get("length")),
                "compass_direction": _f(row.get("compassDirection")),
            }
    return out


def load_stop_scores() -> dict:
    """Return {parent_uic: {"score": float, "tier": str}} from step 06's
    stop_size_scores.json. Empty dict if the file is missing — every dot
    then falls back to the `small_bus` tier and a `WARNING` is printed by
    the caller.
    """
    if not STOP_SCORES.exists():
        return {}
    raw = json.loads(STOP_SCORES.read_text())
    out = {}
    for uic, v in raw.items():
        if isinstance(v, dict):
            out[uic] = {
                "score": float(v.get("score", 0.0)),
                "tier": str(v.get("tier", "small_bus")),
            }
        else:
            out[uic] = {"score": float(v), "tier": "small_bus"}
    return out


def write_stop_attributes_diag(line_stops: dict) -> dict:
    """Build the per-stop attribute lookup + diagnostic for every stop_id that
    appears in any drawn line. Emits stop_attributes_sources.json and returns
    the per-stop dict for downstream consumers (debug overlay, dot placement).
    """
    stop_sloid = load_stop_sloid()
    atlas = load_atlas_attributes()
    print(f"  {len(stop_sloid):,} GTFS stops with SLOID, "
          f"{len(atlas):,} atlas BOARDING_PLATFORM rows")

    used_stop_ids: set = set()
    for ls_entry in line_stops.values():
        triplets = ls_entry.get("stops", []) if isinstance(ls_entry, dict) else ls_entry
        for trip in triplets:
            if len(trip) >= 3 and trip[2]:
                used_stop_ids.add(trip[2])

    out: dict = {}
    n_match = 0
    for sid in used_stop_ids:
        sloid = stop_sloid.get(sid)
        atlas_row = atlas.get(sloid) if sloid else None
        if atlas_row is not None:
            out[sid] = {
                "status": "atlas_match",
                "sloid": sloid,
                "length": atlas_row["length"],
                "compass_direction": atlas_row["compass_direction"],
            }
            n_match += 1
        else:
            out[sid] = {
                "status": "no_atlas_match",
                "sloid": sloid,
            }

    OUT_STOP_ATTRS_DIAG.write_text(json.dumps(out, ensure_ascii=False))
    print(f"  Stop attributes: {n_match:,}/{len(out):,} stops matched atlas "
          f"→ {OUT_STOP_ATTRS_DIAG}")
    return out


def compute_terminus_skip_oids(line_stops: dict,
                                line_lookup: dict | None = None,
                                stop_meta: dict | None = None,
                                radius_m: float = TERMINUS_DEDUP_RADIUS_M):
    """Return `(skip_first_oids, skip_last_oids)`.

    `skip_first_oids` — osm_ids whose FIRST entry (departure terminus) should
    be omitted because another line arrives at the same stop_id within
    `radius_m`. Keeps the arrival side as the visible dot+extent (its extent
    is the non-degenerate one for non-rail) and lets the popup-aggregation
    pass surface both directions.

    Direction-collapsed modes (ferry, aerial mountain, funicular mountain)
    are exempt: their opposite directions are already merged into one
    feature upstream, so any first/last pair at the same stop_id is two
    distinct variants whose pfaedle-snapped endpoints both need dots.

    `skip_last_oids` — tram / bus / regional_bus osm_ids whose LAST entry
    (arrival terminus) is dropped because either (1) the rule above did NOT
    pair it with any departure (layover ~100 m from the real terminus that
    the same line never visits), OR (2) its stop_id has no `platform_code`
    AND some other feature in the same sibling group (ref, agency_id, mode)
    visits the same UIC at a stop_id WITH a `platform_code` — the
    platform-coded entry is the real platform, the bare-numeric layover is
    redundant. `line_lookup` is required to apply rule 1; `stop_meta` is
    additionally required for rule 2.
    """
    def _is_dedup_exempt(oid):
        if not line_lookup:
            return False
        info = line_lookup.get(oid) or line_lookup.get(str(oid))
        if not info:
            return False
        if info.get("mode") == "ferry":
            return True
        if info.get("mode") == "mountain" and info.get("mountain_origin") in ("aerial", "funicular"):
            return True
        return False

    arrivals_by_sid: dict = {}
    departures: list = []
    arrivals_meta: list = []
    for oid, entry in line_stops.items():
        triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
        if not triplets or len(triplets) < 2:
            continue
        first = triplets[0]
        last = triplets[-1]
        if len(first) >= 3 and first[2]:
            departures.append((str(oid), first[2], first[0], first[1]))
        if len(last) >= 3 and last[2]:
            arrivals_by_sid.setdefault(last[2], []).append(
                (str(oid), last[0], last[1]))
            arrivals_meta.append((str(oid), last[2], last[0], last[1]))

    skip_first: set = set()
    departures_by_sid: dict = {}
    for oid_dep, sid, lon_d, lat_d in departures:
        departures_by_sid.setdefault(sid, []).append((oid_dep, lon_d, lat_d))
        if _is_dedup_exempt(oid_dep):
            continue
        for oid_arr, lon_a, lat_a in arrivals_by_sid.get(sid, []):
            if oid_arr == oid_dep:
                continue
            if _is_dedup_exempt(oid_arr):
                continue
            if haversine_km(lon_d, lat_d, lon_a, lat_a) * 1000.0 <= radius_m:
                skip_first.add(oid_dep)
                break

    skip_last: set = set()
    if line_lookup is None:
        return skip_first, skip_last

    sibling_platform_uics: dict = {}
    if stop_meta is not None:
        for oid, entry in line_stops.items():
            info = line_lookup.get(str(oid)) or line_lookup.get(oid)
            if not info or info.get("mode") not in ARRIVAL_DROP_MODES:
                continue
            key = (info.get("ref", ""), info.get("agency_id", ""),
                   info.get("mode", ""))
            triplets = entry.get("stops", []) if isinstance(entry, dict) else entry
            uics = sibling_platform_uics.setdefault(key, set())
            for trip in triplets:
                if len(trip) < 3:
                    continue
                sid = trip[2]
                if not sid:
                    continue
                meta = stop_meta.get(sid)
                if not meta or not meta.get("platform_code"):
                    continue
                uics.add(merge_key_of(sid))

    for oid_arr, sid, lon_a, lat_a in arrivals_meta:
        info = line_lookup.get(oid_arr) or line_lookup.get(str(oid_arr))
        if not info or info.get("mode") not in ARRIVAL_DROP_MODES:
            continue

        # Rule 1: unpaired arrival.
        paired = False
        for oid_dep, lon_d, lat_d in departures_by_sid.get(sid, []):
            if oid_dep == oid_arr:
                continue
            if haversine_km(lon_d, lat_d, lon_a, lat_a) * 1000.0 <= radius_m:
                paired = True
                break
        if not paired:
            skip_last.add(oid_arr)
            continue

        # Rule 2: layover shadowed by same-line real-platform sibling.
        if stop_meta is None:
            continue
        meta = stop_meta.get(sid)
        if meta and meta.get("platform_code"):
            continue
        key = (info.get("ref", ""), info.get("agency_id", ""),
               info.get("mode", ""))
        uic = merge_key_of(sid)
        if uic in sibling_platform_uics.get(key, set()):
            skip_last.add(oid_arr)

    return skip_first, skip_last


__all__ = [
    "load_stop_sloid",
    "load_atlas_attributes",
    "load_stop_scores",
    "write_stop_attributes_diag",
    "compute_terminus_skip_oids",
    "GTFS_STOPS",
    "GTFS_STOPS_PRE_PFAEDLE",
    "ATLAS_CSV",
    "STOP_SCORES",
    "OUT_STOP_ATTRS_DIAG",
    "TERMINUS_DEDUP_RADIUS_M",
    "ARRIVAL_DROP_MODES",
]
