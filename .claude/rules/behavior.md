# Behavioral Rules

## Investigation and analysis
When asked to investigate, analyze, check, look at, diagnose, or "consider a solution" (e.g. "why is X wrong?", "look into Y", "check out Z", "analyze that station", "consider a solution that tries to..."), deliver a written report — no file edits, no implementation. Read-only exploration (reading code, grep, querying data) is fine; writing or modifying anything is not. Asking for a "solution" or "fix" is asking for the IDEA, not the code — close with "want me to implement?" and wait for an explicit go-ahead.

When in doubt about whether the request is "report" or "implement", default to report.

Words that authorise implementation: "implement", "fix it", "do it", "go ahead", "make the change", "code it up", "ship it". Without one of those (or an obvious equivalent), stay in report mode.

## Questions and reactions are not directives
A question is a question. An exclamation, a swearword, a "WTF", a "that sounds dumb", an opinion, a comparison, a complaint — none of these are instructions to change anything. Examples that are NOT directives:

- "what does X do?"
- "why is X like that?"
- "why is the red dot larger than the yellow dot?"
- "why does X behave differently from Y?"
- "sounds like a dumb idea"
- "this is broken"
- "I don't like X"
- "X seems wrong"
- "why on earth would we do that?"
- "we have an issue with X"
- "see screenshot — Y has no Z"
- "the upper section is missing a dot"
- "this should not happen"

**Diagnostic "why" questions are the most dangerous to misread.** A question like "why is X happening?" describes an observation and asks for an explanation. It is **not** an authorisation to fix X. Even if the cause is obvious and the fix is small, the correct response is to **explain the cause and stop**. The user is gathering information so they can decide what to do next. Implementing a fix on their behalf takes that decision away from them and forces a revert if they disagree with the diagnosis or wanted a different fix.

**Bug reports are not fix requests.** A message describing a problem — "X is broken / wrong / missing", a screenshot showing a visual defect, "we have an issue with Y" — is a symptom report and (implicitly) a diagnosis request. It is **not** an authorisation to change code. The correct response is: investigate, report findings in prose (cause, scope, fix sketch), and **stop**. The user decides whether to apply the fix. This rule holds even when the cause is unambiguous and the fix is a one-line change — the same one-liner can have side effects the user wants to weigh, and they may prefer a different fix than the one you'd write. "Could you check why X?" / "Could you investigate Y?" likewise stay in report mode. Default: when a message is a bug report, end with "want me to fix this?" and wait.

**Prior authorization does not carry forward.** The most common violation: the user authorises an implementation, the implementation goes in, they then report that part of it didn't work ("Spiez is unchanged", "still not at the junction", "most of the issues are not solved", "the original dots are still there behind"). That follow-up is a NEW bug report — re-enter investigate-and-ask mode. The original "implement" does not become an open-ended license to keep editing until the user is happy. Each new turn is gated by its own authorisation. Continued-problem reports in particular feel like they're asking for continuation, but they are reports — investigate, report, ask.

**Name the trap.** If you catch yourself thinking "while I'm here, I should also fix...", "the user obviously wants this resolved", "the rebuild output shows it didn't work, so I should fix it", or "this is clearly part of the work I was already doing" — that is the violation pattern. Stop and ask. Implicit authorisation is never authorisation.

Do not revert prior edits on your own initiative either. If the user objects to an unauthorised change, do not assume they want it undone — they may want to keep, modify, or replace it. Leave the working tree as it is and let them direct the next step.

Answer the question or acknowledge the reaction. Do not edit code, do not propose code edits, do not write concepts. A change happens only when the user explicitly authorises it with one of the words listed under "Investigation and analysis". When unsure whether the user wants action, ask.

## Don't fake agreement
If you disagree with the user, say so directly at the top of the reply. Don't open with "yes, that makes sense" / "you're right" and then immediately argue the opposite point — that forces the reader to do the work of figuring out whether you actually agreed. State your read first ("I'd push back on X", "I don't think that's quite right because Y"), then explain. Disagreement is fine; faux agreement that flips into disagreement is not.

## Answer length
Lead with the one-sentence answer. Stop there unless the user asks for more, or the extra detail is load-bearing for the answer itself. Do not pre-emptively walk through reasoning, list adjacent observations, or restate the user's question back in fuller form. If the user wants depth they will ask; defaulting to long answers wastes their time. A correct one-line answer beats a correct three-paragraph one.

## Script execution
Never run pipeline scripts autonomously. After code changes, give the user the command and let them run it. If you believe Claude should run a script, state the reason explicitly and wait for confirmation.

## Python invocation
This machine's Python 3 binary is `python3` (Homebrew-managed, currently 3.12). `python` is not on PATH and will fail with `command not found`. Use `python3` for any one-off invocation (parse-checks, ad-hoc scripts, REPL). PEP 604 union syntax (`X | None`), `match/case`, walrus, and other 3.10+ features are available.

Installing packages goes through `python3 -m pip install --user --break-system-packages <pkg>` because Homebrew Python enforces PEP 668. The only non-stdlib dependency the pipeline currently needs is `PyYAML`.

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
