---
name: Notable config.yaml keys
description: Config keys added during development that are non-obvious or were previously hardcoded
type: project
---

These keys were added to `config.yaml` during basemap v1 development (previously hardcoded in generate_style.py):

**palette:**
- `rail` — rail line color (was hardcoded `#ffffff`)
- `rail_opacity` — rail line opacity (was hardcoded `0.5`)
- `bridge_deck_opacity` — opacity for the bridge deck shape (all zoom levels)

**Previously dead config (now live):**
- `palette.rail` was present in config before but never read by generate_style.py (was hardcoded instead). Now wired up.

**Removed features:**
- Ferry routes excluded from walkable layer via `FERRY_CLASSES` in `WALKABLE_EXCLUDE` — they were rendering as walkability-colored lines over water.
