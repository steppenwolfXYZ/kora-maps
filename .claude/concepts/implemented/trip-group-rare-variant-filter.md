# Trip-Group Rare-Variant Filter

Which variants of a line make it onto the map. Covers the supergroup
rare-group drop, the per-variant share / frequency gate, the seasonal
regional-bus rescue (formerly its own concept), and the per-variant
frequency that drives line thickness.

## Problem

A line's trips are grouped into trip groups (≥2 shared merged stops), and
each group splits into variants by `(merged_stop_set, direction_key)`. Three
kinds of variants must be told apart:

- **Depot runs and non-revenue workings** — a few trips a day, often outside
  daytime hours, frequently ending at a stop the line does not otherwise serve
  (Bern trams 6 / 7 / 8 to Guisanplatz Expo or Eigerplatz, Basel tram 3 to
  Morgartenring, tram 21 to Wiesenplatz). Kept, they draw a spurious branch.
- **Real short-turns and branches** — Bern tram 7 to Kaufmännischer Verband,
  Basel tram 2 to Riehen Grenze, Busland 465 Burgdorf → Fraubrunnen. These
  carry a substantial part of the line's service and must draw.
- **Seasonal mountain feeders** — Theytaz 372 → Le Chargeur, PostAuto 431 →
  Vals Zervreila. They run only in summer or winter and are the point of the
  line, yet look rare on any annual measure.

Two structural failures of the original 10 % / 5 % share rule:

1. Depot runs that share only one stop with the main service are isolated as
   their own trip group, where they are 100 % of the group and can never be
   dropped. (Solved by the supergroup drop below.)
2. The 10 % share assumes one to three main variants plus noise. Rural and
   multi-branch lines fragment into many near-equal terminus pairs: Busland
   465 has 16 variants, the largest at 11.5 %, and the hourly Fraubrunnen
   branch at 9.1 % was dropped although it carries the bulk of the service.
   Dataset-wide, depot runs sit at 0.7–1.7 % share on multi-variant lines but
   reach 3–9 % on lines with few variants, so no share threshold alone
   separates them. Their frequency does: every depot run measured had
   `f_weighted` ≤ 0.15, every real variant ≥ 0.7.

## Requirements

### Supergroup classification

Within each partition `(short_name, agency, bucket)`, trip groups are
classified into **supergroups** by union-find with the predicate "share at
least one merged stop (transitively)". A supergroup is a transient
classification used only for the rare-group drop; it does not change
`trip_group_id` or any other identity used downstream.

Identifier `supergroup_id`, unique within its partition. A trip group belongs
to exactly one supergroup.

### Rare-group drop

A trip group is dropped entirely when its share of its supergroup's weighted
trip count is below 10 %. If no trip group in the supergroup clears 10 %, the
threshold falls back to 5 %. If nothing clears 5 % either, all trip groups in
the supergroup are kept.

No absolute trip-count floor: a depot run can be frequent, so the share
comparison against the supergroup total carries the decision. Dropping a trip
group means none of its variants are emitted, regardless of the per-variant
filter.

### Trip counting

Both the rare-group drop and the per-variant filter count trips by a weight
approximating how often each trip runs over the frequency-sampling window
(number of sample dates the trip's service is active on, the same
`wd_hits + we_hits` quantity the line-level frequency scoring uses), never by
raw distinct `trip_id` count. A depot run modelled as 7 trip_ids on a single
construction date must count far less than 7 trip_ids every weekday.

### Per-variant filter — standard groups

Applies to every trip group that survives the rare-group drop and contains no
`regional_bus_rescued` variant (see below for those). A variant is kept if
**any** of the following holds, evaluated in this order:

1. **Share.** Its weighted share of the group's total ≥ 10 %.
2. **Absolute frequency floor.** Its own `f_weighted` (annual window, per
   direction) > `rare_variant_min_freq`, default **0.3** trips/h.
3. **Relative frequency.** Its own `f_weighted` > `rare_variant_best_fraction`
   × the highest per-variant `f_weighted` in the group, default **0.5**.

If no variant passes any clause, all variants are kept (the group as a whole
is sparse; erasing it would remove a real but rare line).

The former 5 % share fallback is removed. Clause 2 is what re-admits the
hourly Fraubrunnen branch and the Bern / Basel / Genève tram short-turns while
still rejecting every depot run (all at ≤ 0.15). Clause 3 only matters on
lines whose strongest variant is itself below ~0.6 trips/h; there it keeps
2–4-hourly variants of 2–3-hourly rural lines. A fraction of 0.2 was
evaluated and rejected: it admits ~300 further variants at a median of 0.1
trips/h, mostly single school or evening runs, over 40 % of them adding
stations the line does not otherwise serve.

Two new config keys: `rare_variant_min_freq` and
`rare_variant_best_fraction`, both under the transit pipeline config.

### Per-variant filter — rescued-bearing groups

A group containing at least one `regional_bus_rescued` variant (see
"Seasonal regional-bus rescue") uses the following instead of the standard
clauses. The frequency clauses above do **not** apply here: rescued variants
have annual `f_weighted` near zero by construction (a summer-only service
diluted over the whole year), and their groups' strongest variants are
themselves weak (typically 0.2–1.9), so the standard rule would drop nearly
all of them.

- **Per-window share test.** For each of three windows — annual, winter
  (Jan–Mar), summer (Jun–Aug) — compute each variant's weighted share of the
  group's window-restricted total. A variant clears the share test in a
  window if its share ≥ 10 %. No fallback threshold in any window.
- **Kept-by-share set.** A variant passes if it clears the share test in any
  one window. The union of passers forms the "kept-by-share" set.
- **Unique-stop rescue.** A variant outside the kept-by-share set is rescued
  if it serves at least one parent station such that:
  - the station is not served by any kept-by-share variant **across the
    entire dataset** — a depot run often shares part of another line's route
    on its way to the depot, so the check has to be global, AND
  - the station is at least `unique_stop_min_distance_m` from every parent
    station served by **this group's** kept-by-share variants, AND
  - the variant's weighted share ≥ `unique_stop_min_share_pct` in at least
    one of the three windows.
- The rescue is mutual: the kept-by-share set is frozen before the unique-stop
  test runs, so two sub-share variants adding the same unique station both
  pass.

Config keys: `unique_stop_min_distance_m` (default 1000),
`unique_stop_min_share_pct` (default 0.02).

### Seasonal regional-bus rescue

Seasonal mountain-feeder variants fail the variant-level
`min_active_days >= 150` gate because they run one season only. The
threshold cannot be lowered globally for the bus bucket without re-admitting
city construction-replacement services (SVB Bern 3A / 9A), which classify as
city `bus` while the feeders classify as `regional_bus`.

- Config key `min_active_days_regional_bus` (default 90), bus bucket only.
- A bus variant with `active_days < min_active_days` but
  `>= min_active_days_regional_bus` is **rescued**: kept in the variant map
  and tracked in a `regional_bus_rescued` set keyed by `(tg_key, var_key)`.
  Below `min_active_days_regional_bus` it is dropped as `short_active_period`.
- At emission, after final mode classification, a rescued variant whose mode
  is `"bus"` (city bus) is dropped with exclusion reason
  `seasonal_rescue_city_bus`. Rescued variants emitted as `"regional_bus"`
  are kept normally.
- Filter-time diagnostic counter: number of bus variants tentatively rescued,
  printed with the `min_active_days` summary.

### Multi-window freq gate

For any group with at least one `regional_bus_rescued` variant, the
group-level `worst_freq` check evaluates three windows: annual, winter,
summer. Per-window `f_weighted` uses sample dates restricted to the window
and the same window hour counts (07:00–23:00). The group passes if
`f_weighted > worst_freq` in any window. The `freq_score` on emitted features
uses the winning window. Groups without a rescued variant use the annual
window only. Diagnostic: `freq_gate_window_passed: "annual" | "winter" |
"summer" | null`.

### Per-variant freq for line thickness

The `freq_score` written onto each emitted feature is computed from **that
variant's trips only**, not the group's total — what a passenger at a stop in
that direction experiences. For groups that passed the freq gate via a
seasonal window, the same window is used. Applies to every bucket.

**Inclusion is unchanged by this**: the group-level `worst_freq` gate still
uses the group's combined trips. Per-variant freq drives `freq_score` →
`width_base` → thickness and `salience_absolute` → `min_zoom`, and — new with
this revision — the two frequency clauses of the standard per-variant filter.

Kept variants are emitted as separate features, each with its own
frequency; nothing folds a short-turn's trips into the main line's width.
That is a known trade-off, not a bug.

### Diagnostic output

`gtfs_groups_full.json`, per trip group:

- `supergroup_id`, `weighted_trip_count`, `supergroup_weighted_trip_count`,
  `group_share_of_supergroup`, `rare_group_share_threshold` (0.10, 0.05 or
  null on the keep-all path).
- `group_exclusion_reason` value `rare_group_dropped` when the supergroup
  filter drops the group; propagates to every variant's `exclusion_reason`.
- `freq_gate_window_passed` as above.

Per variant:

- `weighted_trip_count`, `variant_share_of_group`.
- `raw_freq`, `f_weighted` — the variant's own annual figures.
- `regional_bus_rescued: true | false`; exclusion reason value
  `seasonal_rescue_city_bus`.
- `rare_variant_passed_by` — **new**, replaces `rare_variant_threshold_pct`:
  `"share" | "min_freq" | "best_fraction" | "keep_all"` for standard groups,
  `"share" | "unique_stop" | "keep_all"` for rescued-bearing groups, `null`
  when dropped.
- `rare_variant_window_passed: "annual" | "winter" | "summer" | null` — the
  window in which a share pass happened (always `"annual"` for standard
  groups, `null` for non-share passes and drops).

Raw `trip_count` / `total_trip_count` stay for backward compatibility.

## Constraints

- **Rare-group drop unchanged.** The supergroup filter keeps its 10 % / 5 %
  / keep-all rule; only the per-variant filter changes. A genuinely
  low-frequency line in a disconnected part of an agency's network is not
  dropped because of a high-frequency same-numbered line elsewhere. A trip
  group sharing a single stop with a much larger service and holding under
  10 % of the supergroup is dropped regardless of its absolute trip count.
- **No currently drawn variant may disappear.** Every variant the old 10 % /
  5 % rule kept passes clause 1 or the keep-all fallback of the new rule
  (verified on the current feed: zero losses).
- **Rescued-bearing groups keep the seasonal branch as it is.** The frequency
  clauses must never be applied to them.
- **Mountain and CC-train exemptions** continue to apply to the rare-group
  drop. Mountain and ferry stay fully exempt from the active-days gate.
  Rail and tram are not subject to the seasonal rescue or the seasonal
  windows — permanent infrastructure implies regular service.
- **Window definitions:** winter = months 1–3, summer = months 6–8. A bus
  active only in December does not pass any seasonal window, by design.
- **Night-only services self-eliminate:** the freq windows count only
  07:00–23:00 trips, so a line with no daytime trips has `f_weighted = 0` in
  every window and fails every frequency clause.
- **Diversion routes may slip through clause 2.** A rerouted variant with
  hourly-ish service for part of the year (Zürich tram 13 via Selnau /
  Stauffacher at 1 % share, 0.32 trips/h) is admitted. Accepted: it draws
  over track other lines already render.
- **The trip-group identity model is unchanged.** `trip_group_id` keeps its
  semantics; the supergroup is a transient annotation.
- **Config validation:** if `min_active_days_regional_bus >= min_active_days`
  the rescue range is empty and the multi-window gates have no candidate
  groups. `rare_variant_min_freq` should stay well above the depot-run band
  (≤ 0.15 in the current feed) and `rare_variant_best_fraction` at or above
  0.5; both were calibrated on the 2026 feed.
