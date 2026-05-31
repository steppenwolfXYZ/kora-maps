# Behavioral Rules

## Issue investigation
When asked to investigate an issue (e.g. "why is X wrong?", "look into Y"), deliver a diagnostic report only — root cause, relevant data, and options. Do NOT implement any changes. Never write or propose code until the concept has been explicitly discussed and agreed upon.

## Script execution
Never run pipeline scripts autonomously. After code changes, give the user the command and let them run it. If you believe Claude should run a script, state the reason explicitly and wait for confirmation.

## Rebuild command
After any transit pipeline change, suggest exactly:
```
./scripts/rebuild_transit.sh --skip-osm
```
Use the flag-less form only when `04_extract_osm.py` or OSM source data has changed. Never suggest running individual Python scripts — the shell script handles the full pipeline.

## Fixing bugs
Fix stop placement bugs by correcting the algorithm in `05_score_and_match.py`, not by tightening snap-distance thresholds in `07_extract_stops.py`. Tightening thresholds papers over a data quality problem instead of fixing it.

## Geo matching scope
`find_best_gtfs_candidate` is for freq/speed selection only. Never feed its canonical stops into stop assignment. Using a single geo-matched candidate in stop assignment causes `_covers_endpoints` to fail more often, triggering the broad geo fallback which pulls in wrong stops. One session: 2 fixes, ~50 regressions introduced this way.

## Transit mode: no intercity category
The `intercity` mode no longer exists. All rail (IC, IR, EC, TGV, ICE, S-Bahn, RE, R, TER, etc.) is classified as `train`. Never reintroduce an `intercity` key anywhere in the pipeline or style code.

## Transit style: casing color
Transit line casing is WHITE (`#ffffff`) for ALL modes including mountain. Never use black. User has confirmed white multiple times.

## Transit style: mountain line
Mountain line color is fixed light yellow `#ffe566` — no frequency-based variance. Width base = 1.0. Do not change these without explicit instruction.

## Bridge deck
Keep exactly one unified `bridge-deck` layer covering all `brunnel=bridge` transportation. Do NOT split into per-class deck layers — they produce hollow "donut" artifacts. This has been tried and reverted multiple times; do not attempt again.

## Concepts
When asked to "write a concept" or before implementing a non-trivial change, create a file in `.claude/concepts/<topic-name>.md`. Name files by topic, not date.

The document is a **requirement definition**, not an implementation guide. Its purpose is to pin down what the change must do before work starts (so requirements don't drift mid-implementation) and to remain as a record of how it was supposed to work once done.

The focus is the requirements. Other sections are short context.

- **Problem** — what is wrong and why it matters. Very short — a few sentences, enough to orient the reader.
- **Requirements** — what the solution must do, including any new identifiers or keys introduced (named explicitly). This is the bulk of the document.
- **Constraints** — edge cases, things that must not change, known exceptions.

Do NOT include implementation steps, code snippets, or file/line references. Those belong in the code, not the concept doc. After implementation, move the file to `.claude/concepts/implemented/` only once the user has explicitly confirmed it is implemented — do not move it on your own assessment. It stays there as the original requirements record.

## Memory / rules
Do not use the auto-memory system. If something is worth remembering across sessions, propose updating a file in `.claude/rules/` and let the user commit it.
