# Realtime updates (delays, cancellations, extra services)

## Problem

Routing answers come from the static timetable only. A user planning a
trip right now sees scheduled times even when the feeder is 10 minutes
late, the train is cancelled, or the platform changed. Switzerland
publishes all of this as GTFS-RT, and the routing backend already knows
how to consume it, so the gap is mostly operational cadence and UI.

Scope of this concept: the routing panel only. Live data on the map
(stop popups with departures, line detail view) comes later.

## Requirements

### Data source

- Provider: opentransportdata.swiss, two feeds, one API key each
  (Bearer token), free tier, attribution "opentransportdata.swiss"
  kept in the app's credits.
  - **Trip Updates** (`gtfs-rt`): delays, cancellations (whole trip),
    skipped stops (partial cancellation), platform changes, extra
    services (added trips). Three-hour preview window, refreshed
    every 30 s at the source, hard limit 2 requests/min per key.
  - **Service Alerts** (`gtfs-sa`): free-text disruption notices in
    DE/FR/IT/EN. Polled from day one so the data is present; its
    display is phase 2 (see Constraints).
- No vehicle positions are published; nothing in this concept depends
  on them.

### Backend

- MOTIS polls both feeds itself from the production server. New
  config: an `rt` list on the `ch` dataset with the two feed URLs and
  their Authorization headers, and `update_interval` set to 30 s (the
  source's refresh rate, and the rate limit's ceiling). No proxy, no
  extra service; the container needs outbound HTTPS only.
- The API keys never enter git. They are injected into the served
  config at deploy time (deploy script reads them from the server's
  environment / a local untracked file).
- The realtime overlay is applied in memory on every poll and must fit
  within the container's existing 2 GB budget. Verified locally before
  the production rollout.
- **The static index must track the feed's releases.** Trip ids are
  only valid for the currently active static release; the live feed
  switches to the newest release Monday and Thursday at 15:00, the
  release itself appears 09:00–10:00 the same day. The Kranich routing
  build therefore runs on every release day, triggered after
  publication and finishing before 15:00. Missing a release degrades
  gracefully (updates stop resolving, results fall back to schedule
  only) but is a monitored condition, not an accepted one.
- The client learns the age of the server's realtime overlay with
  every result set (new response-level field `rtAge`, seconds since
  the last successful poll; absent when realtime is not configured).

### Client polling

- While a result list is open and its date is today, the client
  re-queries every 30 s. Poll timing is aligned to the server's cycle
  so each poll sees fresh data rather than the same overlay twice:
  the client uses `rtAge` to phase its next request just after the
  server's next refresh.
- Polling pauses when the tab is hidden and resumes with an immediate
  poll on return. Results restored from session persistence resume
  polling too.
- Queries for other days, or more than three hours ahead, are not
  polled; they show no realtime marks at all.

### Automatic poll vs. explicit update

Two distinct behaviours, deliberately separated:

- **Automatic polls annotate in place.** The list keeps its order and
  its cards. Each poll may only: update times and delays, update
  platforms, add or clear warnings, mark connections as broken. It
  never reorders, removes, or adds connections.
- **"Update results" replaces the list.** A button, shown in the
  broken-connections bar (below), triggers a full re-query with the
  normal ranking, so cards may move, disappear, or appear.

### Delays

- A leg whose realtime departure or arrival differs from schedule by
  ≥ 1 minute shows the actual time in brand red, with the scheduled
  time struck through next to it. Under 1 minute counts as on time.
- Applies wherever that time is displayed: the card overview (departure,
  arrival, total duration) and the expanded leg list.
- A delayed connection carries a delay icon in the warning-icon section
  of its card.
- **On-time confirmation:** a connection whose legs all have realtime
  data and are all on time shows a green tick in the warning-icon
  section. The tick is the signal that live data exists; a connection
  without any realtime data shows nothing.
- Mixed case (some legs with live data, some without): delays shown
  where known, no tick.

### Platform changes

- A leg whose realtime platform differs from schedule shows the new
  platform in brand red with the old one struck through, in overview
  and detail alike.

### Extra services (added trips)

- Added trips take part in routing like any other trip.
- A leg on an added trip carries a green marker "extra service"
  (German: "Zusatzverbindung"); the connection carries the same marker
  in its warning-icon section, also in green.
- Added trips have no drawn line on the map, so their badge colour is
  the mode fallback colour already used for undrawn routes.

### Cancellations and broken connections

- A shown connection becomes **broken** when a poll reports that it no
  longer works: a leg's trip is cancelled, its boarding or alighting
  stop is skipped, or a delay makes a transfer infeasible under the
  connection-warning ladder (see below).
- Broken connections stay in the list, get a red background and a
  "broken" warning in the warning-icon section, and remain expandable
  so the user can see which leg failed.
- While at least one shown connection is broken, a bar appears at the
  top of the list: "Because of delays or cancellations, some of your
  connections won't work any more." with the **Update results**
  button.
- After an explicit update, the new list opens with a notice on top
  when connections from the previous list were dropped for
  cancellations: "Some results were skipped because of cancelled
  connections." A "details" affordance listing exactly which ones is
  phase 2.
- Skipped intermediate stops in a leg's stop list are struck through,
  not hidden.

### Change notifications

- When an automatic poll changes something visible (a delay crosses
  the 1-minute threshold, a platform changes, an on-time confirmation
  appears) a short toast says live data was updated. One toast per
  poll, not per change.
- When a poll breaks a connection, no toast; the bar above is the
  notification.

### Connection warnings on live times

- The existing tight-transfer ladder computes on actual times wherever
  realtime is present for both legs of a transfer, scheduled times
  otherwise. Tier wording and thresholds are unchanged.

### Freshness indicator

- Nothing is shown while polling works as planned.
- When the last successful poll (client-side) or the server's overlay
  age (`rtAge`) exceeds 2 minutes, a gray bar above the list says live
  data is not up to date. It disappears with the next successful poll.

### Language

- All new strings follow the app language: English now, German next.
  German wording fixed here: "Zusatzverbindung" for extra services.

## Constraints

- Map rendering is out of scope: no live vehicle positions, no live
  departures in stop popups, no changes to the line detail view.
- Service-alert texts (free-text notices from the `gtfs-sa` feed) are
  polled but not displayed in phase 1. Phase 2 decides placement
  (inline on the leg, banner, station chip) and collapsing.
- The "details" list of skipped cancelled connections after an update
  is phase 2.
- Automatic polls must never move or remove a card; only the explicit
  update does. Card identity across polls is keyed on scheduled times
  and trip ids, never on actual times, so a delay does not turn a card
  into a new one.
- Trips split by the pipeline's own overrides (two-vehicle services
  encoded as one GTFS trip) do not match realtime updates. Accepted.
- Realtime never changes the persisted routing state, recent-routes
  entries, or the URL: those keep scheduled endpoints, date and time.
- Nothing in the client ever calls the opentransportdata API; the key
  stays on the server.
