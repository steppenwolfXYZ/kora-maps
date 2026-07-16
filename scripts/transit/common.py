"""Shared cross-step utilities: paths, config loading.

Kept small on purpose — anything genuinely reused by multiple pipeline
steps belongs here. Anything used by only one step stays local to that
step.
"""

from pathlib import Path

import yaml

TRANSIT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TRANSIT_DIR.parents[1]
CFG_PATH = TRANSIT_DIR / "config.yaml"

_CFG_CACHE: dict = {}


def load_transit_cfg() -> dict:
    """Return the parsed scripts/transit/config.yaml. Cached across calls."""
    if "cfg" not in _CFG_CACHE:
        _CFG_CACHE["cfg"] = yaml.safe_load(CFG_PATH.read_text())
    return _CFG_CACHE["cfg"]
