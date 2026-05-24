# Memory Index

- [Project Overview](project_overview.md) — stack, key files, tile source, basemap v1 status
- [Style Architecture](style_architecture.md) — layer build functions, render order, road class constants, bridge deck design
- [Bridge Deck Rendering Lessons](feedback_bridge_deck.md) — what works, what doesn't, MapLibre constraints, do not retry per-class decks
- [Notable config.yaml keys](config_keys.md) — keys added during v1, previously hardcoded values now wired to config
- [Transit Color Scheme](transit_color_scheme.md) — mode categories, colors, speed/frequency encoding, Swiss rail classification
- [Transit Style Feedback](feedback_transit_style.md) — casing=white always, mountain=light yellow fixed, width=1.0
- [Transit intercity/train parity](feedback_transit_intercity.md) — never differentiate intercity vs train in style formulas
- [Open Tasks](tasks_open.md) — current bugs and open issues in transit layer pipeline
- [Mountain Pipeline](mountain_pipeline.md) — GTFS-first cable car architecture, OSM geometry lookup, stop rendering, known issues
- [Transit Rebuild Workflow](feedback_rebuild_workflow.md) — use --skip-osm unless 04_extract_osm.py or OSM data changed
- [Script Execution Approval](feedback_run_scripts.md) — always ask user before running pipeline scripts; state reason if Claude should run it
- [Fix approach — correct data, not thresholds](feedback_fix_approach.md) — fix stop assignment bugs in 05_score_and_match.py, not by tightening downstream snap thresholds
- [OSM→GTFS Matching Architecture](matching_architecture.md) — _line_canonical_export tuple format, find_best_gtfs_candidate, geo-tiebreaker logic, fallback chain
- [Geo matching scope](feedback_geo_matching.md) — find_best_gtfs_candidate is for freq/speed only; never feed its stops into stop assignment
- **After any transit pipeline change: suggest `./scripts/rebuild_transit.sh --skip-osm` — never individual scripts. Never run it yourself.**
