# Routing Persistence

## Problem

Configuring a route (from, to, date/time, arrive/leave) takes real effort, but the result is fragile: one browser back or one click on the panel's ✕ and everything is gone. Users without an account (accounts come much later) currently have no way to get a previous route back short of re-entering it.

## Definitions

- **Route set** — both a from AND a to endpoint are finally set; a route is loaded or currently loading.
- **No route set** — the endpoint inputs may contain text, but at most one endpoint is finally set; no route is loaded or loading.

These two states drive the panel layout (see Requirements).

## Requirements

### Restore on reopen

- Closing the routing panel (✕ or browser back) never discards the routing state. Reopening the panel within the same session restores exactly what was there — endpoints, date/time, arrive/leave mode, and the loaded results. No re-query on an in-session restore.
- Cold loads keep working through the existing URL restore: loading a routing URL re-queries with the URL's date/time, **even when that date/time is in the past**. Shared-connection URLs are unchanged.

### Clear-route button

- When a route is set, the panel shows a **clear route** button at the top — right of the title, above the from input.
- Clearing resets to the no-route-set state (empty endpoints, no results). It does not remove anything from the recents list.

### No-route-set view

When no route is set, a **recent routes** section appears below the date/time controls: a list of the most recent routes the user has shown. (The date/time and arrive/leave controls stay visible — hiding them until both endpoints were set was tried and reverted after testing.)

### Recent routes list

- **Stored in `localStorage`** (not a cookie) under the key `kora.routing.recents`, so it survives reloads and works without an account.
- An entry is recorded (or refreshed to the top) whenever a route is **shown** — a query for a from/to pair successfully returns results. This includes routes loaded from URLs.
- Each entry carries: both endpoints (display name + whatever identity is needed to re-query, incl. custom coordinate endpoints), date/time, and arrive/leave mode.
- Deduplication by from/to pair: showing an already-listed pair moves it to the top and updates its date/time and mode; the list never contains the same pair twice.
- Capacity: the 10 most recent pairs; older entries drop off.
- **Selecting an entry re-queries.** Date/time handling on selection:
  - Stored date/time still in the future (or now): keep it, keep the arrive/leave mode.
  - Stored date/time in the past: switch to "now" (depart now).

### Later version (out of scope here, recorded as direction)

- The no-route-set view grows tabs: **Recent** (this list) and a **Touch timetable** — SBB-app-style saved favorite connections, tappable to query instantly. Possibly localStorage first, possibly account-backed.

## Constraints

- Past date/time is only ever replaced with "now" on **selection from recents**. URL loads and in-session restores keep the stored date/time untouched, past or not.
- No cookies; nothing routing-related is sent to the server for persistence. localStorage must degrade gracefully (private mode / blocked storage → feature silently absent, panel still fully usable).
- The existing share flow, URL format, and browser-back behavior for the itinerary selection stay unchanged.
- Recents store no query results — only the inputs needed to re-run the query.
