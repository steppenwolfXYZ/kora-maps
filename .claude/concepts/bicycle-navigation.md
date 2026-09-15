# Bicycle Navigation

Live turn-by-turn navigation for a planned cycling route: the map
follows the rider, a banner shows the next maneuver, and the route is
recomputed when the rider leaves it. Cycling only — walking navigation
is a separate later project with its own requirements.

## Problem

The cycling tab plans a route but cannot guide a ride. Once the rider
sets off, the phone screen locks, the map stays where it was, and a
missed turn leaves the drawn route behind with no correction. The
engine already returns the maneuvers a guidance mode needs; what is
missing is the mode itself.

## Requirements

### 1. Entering and leaving

- Navigation can be started from two places, both offered whenever a
  cycling route is drawn: a button in the map's top-right control area
  and a button on the selected route card. Both start navigation for
  the currently selected route (alternatives are not navigated).
- Starting requires a location fix. Navigation asks for the position
  when the user presses start (never earlier) and, if the permission
  is denied or no fix arrives within the existing timeout, stays in
  planning mode and shows the existing location error message.
- Starting from a route whose start is not the current location is
  allowed: the first instruction then guides the rider onto the route,
  and the off-route recalculation (§ 4) takes over as soon as the
  rider has a fix, so the route is effectively re-planned from where
  the rider actually is.
- Leaving: a single × button ends navigation and returns to the
  planning view with the route still drawn and selected. Navigation
  also ends automatically on arrival (§ 3). There is no confirmation
  dialog.
- Navigation state (active, selected route, destination) is kept
  locally so a page reload during a ride resumes navigation rather
  than dropping back to planning. It is **not** encoded in the URL —
  a shared link never opens in navigation mode.

### 2. Follow-me map

- While navigating, the camera follows the rider's position: heading
  up (the map rotates so the direction of travel points to the top of
  the screen), tilted, and zoomed to a close street-level view. The
  rider's position sits in the lower part of the viewport so most of
  the screen shows what lies ahead.
- **Route lock.** While the position is within a few metres of the
  route (tighter than the off-route distance, widened only by a poor
  accuracy figure), the marker sits on the projected point of the route and
  the arrow points along the route there — a turn shows the instant the
  projection passes the corner, with no lag and no jitter. All
  decisions (off-route, switching to an alternative, arrival) keep
  using the raw position.
- Off the route, heading comes from the GPS course whenever the rider
  is moving fast enough for the course to be meaningful; otherwise from
  the bearing of the rider's own movement over the last two seconds —
  a time window, so walking and riding resolve a turn equally fast —
  gated only against position jitter. Below that (standing at a light)
  the compass heading is used if the device offers one. GPS course
  always wins over the compass while it is valid — compasses are
  frequently miscalibrated. Standing still without a compass for a few
  seconds drops the heading, on or off the route: the marker shows the
  plain position dot until movement resumes — an arrow always means a
  known direction.
- Heading and position changes are smoothed so the map does not
  jitter between fixes.
- **Dynamic zoom and tilt.** The camera frames the road up to the next
  change of direction: on the approach it sits far enough out that the
  upcoming maneuver point is in view, it tightens as the rider reaches
  the turn, and once the turn is passed it widens again toward the
  following one. Speed adds a second term — faster riding sits a little
  further out. Tilt follows zoom: flatter when far out, steeper when
  close. Zoom and tilt stay within a fixed near/far band, change with
  hysteresis so noisy distances never make the camera hunt, and ease
  continuously between fixes rather than stepping.
- The rider is drawn as a distinct position marker with a direction
  indicator; the existing route pins stay. While following, that
  marker is a fixed element on the screen — large, in the lower part of
  the viewport with only padding below it — and the map glides
  underneath, so it never jumps between position fixes. While following
  is suspended it is a marker on the map at the rider's position. The
  handover between the two (start, re-center) is one continuous
  motion: the map marker rides the camera move into place, growing to
  the fixed arrow's size, and the fixed element appears only once the
  camera is at rest exactly there. The
  locate control's own dot is hidden during navigation (the control
  stays active).
- **Panning away:** any map gesture (drag, pinch, rotate) suspends
  following without ending navigation. A "re-center" control appears
  while following is suspended; tapping it resumes following. The
  maneuver banner keeps updating while following is suspended.
- The panel and chrome are reduced to what a rider needs at a glance:
  the maneuver banner, the trip summary (§ 3), the × button and the
  re-center control. The routing panel, search bar and menu are hidden
  while navigating.

### 3. Maneuver banner and trip summary

- A banner at the top of the screen shows the **next maneuver**: a
  turn icon, the distance to it, and the instruction text with the
  street or path name. When the next maneuver is very close, the one
  after it is previewed in a smaller secondary line so the rider can
  prepare for two quick turns in a row.
- Instruction text comes from the engine's turn-by-turn instructions,
  requested in English; units are metric. Distances round to values a
  rider can act on (5 m steps below 100 m, 10 m steps below 1 km, then
  100 m steps).
- Pushed-bike sections and stairs are announced as maneuvers in their
  own right ("push your bike", "stairs"), consistent with the dotted
  rendering of those sections on the map.
- A compact trip summary shows **remaining distance**, **remaining
  time** and **estimated arrival time**, all recomputed as the rider
  advances. Remaining values derive from the rider's projected
  position along the route, not from the last recalculation.
- The trip summary also names the destination as the rider entered
  it.
- **Arrival:** when the rider is within a short distance of the
  destination, the banner switches to an arrival state showing the
  destination name large — that is what the rider is now looking for —
  and offers a Finish button. Navigation never ends on its own; Finish
  or the × ends it.

### 4. Off-route detection and recalculation

- The rider's position is continuously projected onto the route. The
  rider counts as off-route once the projected distance exceeds
  **30 m** for at least **5 s** (a single bad fix must never trigger a
  recalculation). Position accuracy is taken into account: a fix whose
  reported accuracy is worse than the off-route distance is not
  evidence of being off-route.
- A recalculation requests a new route from the current position to
  the original destination with the same options the planned route
  used (avoid-stairs, walk/ride speed), and from the rider's current
  direction of travel: turning back is a priced U-turn the engine
  reports as the first maneuver, never a silent reversal. Via points
  already passed are dropped; those still ahead are kept.
- Recalculations are rate-limited to **one per 10 s** at most, so a
  rider wandering through a square does not fire a burst of requests.
- The new route replaces the navigated route on the map and in the
  banner without interrupting following. Alternatives are requested
  alongside a recalculation and drawn per § 4a; they never replace the
  navigated route on their own.
- Until a recalculation succeeds, guidance continues on the old route:
  the banner shows the next maneuver of the old route relative to the
  rider's projection, and the summary marks itself as approximate.

### 4a. Live alternatives

- While navigating, the alternatives to the current route are drawn on
  the map in the same muted treatment the planning view uses for
  alternatives. They are not interactive — a rider's hands are on the
  bars.
- **Taking an alternative is done by riding it.** When the rider leaves
  the navigated route, the off-route check first tests whether the
  rider is following a shown alternative; if so, that alternative
  becomes the navigated route on the spot, with no request to the
  engine. Only a rider on neither route triggers a recalculation.
- Each shown alternative carries a small bubble near the point where it
  parts from the navigated route, stating the time difference relative
  to it ("+3 min", "−1 min"). The bubble is styled like the app's own
  chrome, not a map label.
- Alternatives are refreshed on every recalculation and whenever the
  rider passes the point where the current alternative diverges — that
  point is behind them, so the alternative is spent. No polling, no
  refresh on a timer. Shown alternatives are **sticky**: a refresh may
  add to the set but removes one only when it is spent, taken, or has
  become the navigated route itself.
- No alternatives within the last few hundred metres before the goal:
  none are fetched and none shown.
- One alternative at a time is enough; a second is tolerable. The one
  shown should diverge **early**: alternatives that part from the route
  further ahead will be offered by a later refresh anyway. When nothing
  diverges early enough, nothing is shown — alternatives are never
  forced.
- Alternatives never change the banner: guidance always follows the
  navigated route until the rider has actually switched.

### 5. Keeping the screen on

- While navigation is active and the page is in the foreground, the
  screen must not lock (screen wake lock). The lock is released when
  navigation ends.
- Foreground only: when the tab is backgrounded or the device is
  locked, guidance pauses. On return to the foreground the wake lock
  is re-acquired, the position is refreshed, and following resumes
  from the current fix.
- On browsers without wake-lock support, navigation still works; the
  rider is told once that the screen may lock.

### 6. Connection loss

- Guidance itself never depends on the network: following, the
  maneuver banner, the trip summary and arrival detection all run on
  the already-loaded route. Only recalculation needs a request.
- A failed recalculation (offline, timeout, server error) keeps the
  old route and retries with increasing intervals while the rider
  stays off-route. A discreet banner states that the route could not
  be updated; it disappears on the first successful recalculation.
- Map tiles that cannot be loaded leave blank areas; the route line,
  position marker and banner remain usable. Offline caching of tiles
  and routes belongs to the planned service-worker project, not here.

### 7. Battery and sensors

- Position updates use the high-accuracy mode of the browser
  geolocation API for the duration of navigation and are stopped the
  moment navigation ends — planning mode keeps today's on-demand
  behaviour.
- Compass access is requested only when navigation starts and only
  where the platform requires an explicit permission; a denied compass
  degrades to GPS-course-only heading silently.

## Constraints

- Cycling tab only. The walking and transit tabs get no navigation
  entry point; the transit tab's behaviour and request shape are
  untouched.
- No audio guidance. Voice instructions are explicitly out of scope
  and the banner must not assume they exist.
- Route planning (query, alternatives, cards, costing) is unchanged;
  navigation consumes the selected route and its maneuvers as planned.
  Requesting instruction text for cycling routes must not change the
  routes themselves.
- The map's existing position-hash URL sync, view modes and layer
  toggles keep working; navigation only changes the camera and the
  chrome while active, and restores the previous camera behaviour on
  exit.
- Recalculations go through the same public route endpoint as
  planning; no new engine actions are exposed.
- Location is never requested before the rider presses start, in line
  with the app's existing geolocation policy.
- Labels English only; i18n out of scope.
