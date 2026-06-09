# Behavioral Rules

## Investigation and analysis
When asked to investigate, analyze, check, look at, diagnose, or "consider a solution" (e.g. "why is X wrong?", "look into Y", "check out Z", "analyze that station", "consider a solution that tries to..."), deliver a written report — no file edits, no implementation. Read-only exploration (reading code, grep, querying data) is fine; writing or modifying anything is not. Asking for a "solution" or "fix" is asking for the IDEA, not the code — close with "want me to implement?" and wait for an explicit go-ahead.

When in doubt about whether the request is "report" or "implement", default to report.

Words that authorise implementation: "implement", "fix it", "do it", "go ahead", "make the change", "code it up", "ship it". Without one of those (or an obvious equivalent), stay in report mode.

## Questions and reactions are not directives
A question is a question. An exclamation, a swearword, a "WTF", a "that sounds dumb", an opinion, a comparison, a complaint — none of these are instructions to change anything. Examples that are NOT directives:

- "what does X do?"
- "why is X like that?"
- "sounds like a dumb idea"
- "this is broken"
- "I don't like X"
- "X seems wrong"
- "why on earth would we do that?"

Answer the question or acknowledge the reaction. Do not edit code, do not propose code edits, do not write concepts. A change happens only when the user explicitly authorises it with one of the words listed under "Investigation and analysis". When unsure whether the user wants action, ask.

## Don't fake agreement
If you disagree with the user, say so directly at the top of the reply. Don't open with "yes, that makes sense" / "you're right" and then immediately argue the opposite point — that forces the reader to do the work of figuring out whether you actually agreed. State your read first ("I'd push back on X", "I don't think that's quite right because Y"), then explain. Disagreement is fine; faux agreement that flips into disagreement is not.

## Answer length
Lead with the one-sentence answer. Stop there unless the user asks for more, or the extra detail is load-bearing for the answer itself. Do not pre-emptively walk through reasoning, list adjacent observations, or restate the user's question back in fuller form. If the user wants depth they will ask; defaulting to long answers wastes their time. A correct one-line answer beats a correct three-paragraph one.

## Script execution
Never run pipeline scripts autonomously. After code changes, give the user the command and let them run it. If you believe Claude should run a script, state the reason explicitly and wait for confirmation.

## Python invocation
This machine's Python 3 binary is `python3`. `python` is not on PATH and will fail with `command not found`. Use `python3` for any one-off invocation (parse-checks, ad-hoc scripts, REPL).

## Rebuild command
After any transit pipeline change, suggest `./scripts/rebuild_transit.sh --start N` where N is the lowest step whose inputs you actually changed (see the step list in `.claude/rules/transit.md`). Each step's output is the next step's input, so `--start N` runs steps N..8 contiguously — starting lower than needed just wastes time, especially on pfaedle. Never suggest running individual Python scripts.

## Fixing bugs
Fix stop placement bugs by correcting the algorithm in `06_score_and_match.py`, not by tightening snap-distance thresholds in `07_extract_stops.py`. Tightening thresholds papers over a data quality problem instead of fixing it.

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
