"""SLOID stop identity (see sloid-stop-identity.md).

Since the 2026-06-04 SLOID migration the feed's Swiss stop_ids are
SLOID-based (`ch:1:sloid:10:0:19`) and the station's UIC number lives in
the `didok` column — it can no longer be parsed out of the stop_id.
pfaedle strips non-standard columns, so step 04 bakes a per-stop
identity table (`data/gtfs_filtered/stop_identity.json`) that every
post-pfaedle consumer reads instead of guessing from IDs.

Three granularities (terminology from the concept):
  station — StopPlace SLOID (`ch:1:sloid:10`), UIC via `didok`
  track   — quay stop (`ch:1:sloid:10:0:19`), platform_code = "19"
  sector  — `_gen:` variant (`…_gen:ch:1:sloid:10:0:19_pf:19A-D`),
            platform_code = "19A-D", referencing its quay when known

Entry shape (all strings, "" when unknown):
  {stop_id: {"uic":     station UIC number,
             "station": station SLOID (or legacy UIC for foreign stops),
             "track":   public track/stop code,
             "sector":  sector-range code, only on sector variants,
             "quay":    the referenced quay stop_id, only on sector
                        variants whose _gen part names a real SLOID,
             "parent":  parent stop_id (without the "Parent" prefix)}}
"""
import csv
import json
import re
from pathlib import Path

from common import PROJECT_ROOT

IDENTITY_PATH = PROJECT_ROOT / "data" / "gtfs_filtered" / "stop_identity.json"

# Leading track part of a sector-range code: "19A-D" → "19", "2A" → "2".
# Codes with no digit prefix ("A", "B-C") keep themselves as the track.
_TRACK_PREFIX_RE = re.compile(r"^(\d+)")


def _station_sloid(sid: str) -> str:
    """Station part of a stop_id: SLOIDs keep their first four segments,
    legacy numeric IDs their leading digits, others themselves."""
    sid = sid.removeprefix("Parent")
    if sid.startswith("ch:1:sloid:"):
        return ":".join(sid.split(":")[:4])
    return sid.split(":")[0]


def _track_from_code(code: str) -> str:
    m = _TRACK_PREFIX_RE.match(code)
    return m.group(1) if m else code


def build_identity(stops_rows) -> dict:
    """Build the identity table from filtered stops.txt DictReader rows.

    Two passes: sector variants resolve their track code from the
    referenced quay's platform_code where possible, falling back to the
    numeric prefix of their own sector code.
    """
    out: dict = {}
    for row in stops_rows:
        sid = row["stop_id"]
        if sid.startswith("WPT:"):
            continue
        uic = (row.get("didok") or "").strip()
        if not uic:
            # Legacy / foreign scheme: UIC-prefixed numeric stop_id.
            head = sid.split(":")[0].split("_")[0]
            if head.isdigit():
                uic = head
        parent = (row.get("parent_station") or "").strip().removeprefix("Parent")
        pc = (row.get("platform_code") or "").strip()

        if "_gen:" in sid:
            station_part, rest = sid.split("_gen:", 1)
            mid, _, pf = rest.partition("_pf:")
            quay = mid if mid and mid != "missingSLOID" else ""
            sector = pc or pf
            entry = {
                "uic": uic,
                "station": _station_sloid(station_part),
                "track": "",  # second pass
                "sector": sector,
                "quay": quay,
                "parent": parent,
            }
        else:
            entry = {
                "uic": uic,
                "station": parent if parent else _station_sloid(sid),
                "track": pc,
                "sector": "",
                "quay": "",
                "parent": parent,
            }
        out[sid] = entry

    for sid, e in out.items():
        if not e["sector"]:
            continue
        quay_entry = out.get(e["quay"]) if e["quay"] else None
        if quay_entry and quay_entry["track"]:
            e["track"] = quay_entry["track"]
        else:
            e["track"] = _track_from_code(e["sector"])
        if not e["uic"] and quay_entry:
            e["uic"] = quay_entry["uic"]
    return out


def write_identity(identity: dict) -> None:
    IDENTITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = IDENTITY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(identity, separators=(",", ":")))
    tmp.replace(IDENTITY_PATH)


_identity_cache: dict | None = None


def load_identity() -> dict:
    """Load (and cache) the step-04 identity table. Missing file fails
    loudly — running steps 6+ against a feed whose identity table was
    never built would silently degrade every UIC-keyed artifact."""
    global _identity_cache
    if _identity_cache is None:
        if not IDENTITY_PATH.exists():
            raise FileNotFoundError(
                f"missing {IDENTITY_PATH} — re-run pipeline step 4 "
                f"(04_preprocess_gtfs.py writes the stop identity table)"
            )
        _identity_cache = json.loads(IDENTITY_PATH.read_text())
    return _identity_cache


def uic_of(sid: str) -> str:
    """Station UIC for a stop_id. Identity table first; legacy numeric
    prefix as fallback for IDs the table doesn't know (old-scheme
    artifacts, foreign feeds); "" when neither applies — callers treat
    that as 'stands alone'."""
    e = load_identity().get(sid)
    if e and e["uic"]:
        return e["uic"]
    head = sid.split(":")[0].split("_")[0]
    return head if head.isdigit() else ""


def merge_key_of(sid: str) -> str:
    """Station-level merge key (the identity model's 'merged UIC'):
    the UIC when known, else the parent, else the stop itself."""
    e = load_identity().get(sid)
    if e:
        return e["uic"] or e["parent"] or sid
    head = sid.split(":")[0].split("_")[0]
    return head if head.isdigit() else sid


def draw_id_of(sid: str) -> str:
    """Track-granularity stop id for map rendering: sector variants
    collapse onto their referenced quay; everything else draws as
    itself (see sloid-stop-identity.md § Track vs sector)."""
    e = load_identity().get(sid)
    if e and e["quay"]:
        return e["quay"]
    return sid


def track_code_of(sid: str) -> str:
    """Public track/stop code for a stop_id ("" when none)."""
    e = load_identity().get(sid)
    return e["track"] if e else ""
