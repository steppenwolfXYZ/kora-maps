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
- **Current-location endpoints are never stored as such** — a live "current location" can't reproduce the shown result later. At record time the coordinate the query actually resolved is stored as a point endpoint, reverse-geocoded to an address like the map right-click (nameless coordinate fallback when the geocoder doesn't answer in time). If no resolved coordinate exists, the entry is skipped. Legacy stored entries with a current-location endpoint are filtered out on read.
- Deduplication by from/to pair: showing an already-listed pair moves it to the top and updates its date/time and mode; the list never contains the same pair twice.
- Capacity: the 30 most recent pairs; older entries drop off. The list shows 10 collapsed, with a "Show more" button revealing the rest (collapses again on panel reopen).
- **Selecting an entry re-queries.** Date/time handling on selection:
  - Stored date/time still in the future (or now): keep it, keep the arrive/leave mode.
  - Stored date/time in the past: switch to "now" (depart now).

### Connect (station tile grid)

The no-route-set view grows two tabs: **Connect** (default) and **Recent** (the list above). The name is "Connect" — not "touch timetable", though the SBB app's touch timetable is the reference interaction. The selected tab persists in `localStorage` under `kora.routing.suggestTab`.

- Connect is a **lined grid** of station cells (grid lines, not buttons). Interaction is **drag-to-connect**: press a cell, drag — a line follows the pointer — release on another cell, and the route is set (start cell = From, release cell = To).
- **Capacity: 10 station options**, plus a fixed **bottom row** (SBB-style): a **Current location** cell (hidden when geolocation is unavailable or denied) and two **empty half-cells**, "Start" and "Stop". A connection drawn through an empty cell leaves that side of the route empty — the station on the other end of the line fills the opposite side, and the cursor lands in the empty side's input.
- **Tile colors**: station tiles are filled with a 135° gradient from the station's average line color to its dominant line color (`ca` / `cd`, baked into `stop_search_index.json` by step 07), white text/icons; fallback with an older index is a tint→tone of the mode mid-color, then anthracite. The bottom-row cells are flat fills: blue-tinted gray for Current location, a lighter gray for Start, a darker one for Stop.
- **Real options**: the user's most-used stations, stored in `localStorage` under `kora.connect.stations`. Every station endpoint of a shown route bumps its usage; ranking is by recency-decayed frequency (half-life on the order of months). Stations only — point/address endpoints don't tile pre-account.
- **Cold start**: while fewer than 10 real options exist, the remaining slots are filled with standard suggestions computed from location:
  - the closest station,
  - the closest station of each stop tier higher than the closest one's tier,
  - the 4 closest stations of the highest tier (major train stations).
  Deduplicated, sorted by distance, capped to the free slots. Real options always displace suggestions as usage accumulates.
- **Anchor** for "closest": the map center. Geolocation is never requested on panel open (house rule — location only on explicit user action); the map center is where the user is looking and needs no permission.

### Later version (out of scope here, recorded as direction)

- Account-backed sync of recents and Connect usage data.

## Constraints

- Past date/time is only ever replaced with "now" on **selection from recents**. URL loads and in-session restores keep the stored date/time untouched, past or not.
- No cookies; nothing routing-related is sent to the server for persistence. localStorage must degrade gracefully (private mode / blocked storage → feature silently absent, panel still fully usable).
- The existing share flow, URL format, and browser-back behavior for the itinerary selection stay unchanged.
- Recents store no query results — only the inputs needed to re-run the query.
