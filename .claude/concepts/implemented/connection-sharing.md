# Connection Sharing

## Problem

A found connection cannot be shared. Sharing the routing URL re-runs the search and shows five fresh connections — the specific connection the sender meant is not guaranteed to appear, and the link has no preview image. Users want to send one exact connection to someone else with a rich link preview.

## Requirements

### Share button
- Each connection card in the routing results gets a share button, placed directly left of the existing map button. Material Symbols filled icon (`share`).
- Tapping it creates a share on the server and hands the resulting short URL to the user: native share sheet (`navigator.share`) where available, clipboard copy with confirmation otherwise.

### Share creation and storage
- New app-server endpoint `POST /api/share`: receives the full serialized connection (all legs with trip identities, stop identities, planned times, modes, line refs and drawn colors — enough to re-render the card, render the og:image, and strict-match against a later MOTIS re-query) plus the original query context (from/to place tokens) needed to re-issue the query.
- The server stores one JSON file and one pre-rendered PNG per share in a dedicated shares directory on the server (outside the app build, so deploys don't wipe it). File storage is deliberate: pragmatic now, trivially migratable to a database later (one record per share).
- Share ID: short random identifier (~8 chars, URL-safe alphabet), not guessable-by-enumeration. The share URL is `koramaps.app/s/<id>`.
- The og:image PNG is rendered once at creation time, not on demand — after deletion the image URL simply stops resolving.

### Preview image (og:image)
- Content: the connection header card as rendered in the routing panel — time range, total duration, origin/destination names with times, the leg badge chain with the exact drawn line colors, transfers + walking summary — plus the Kora Maps logo. No map, no map button.
- Rendered server-side from the committed Saira fonts; social-preview dimensions (1200×630).
- Per-share meta tags served with the `/s/<id>` page: `og:title` (e.g. "Bern 09:34 → Vounetse 11:28"), `og:description` (duration, transfers), `og:image`.

### Shared view
- Opening `/s/<id>` shows the map with the routing view open in single-connection mode: only the shared connection is listed.
- The earlier/later buttons remain visible and functional; using them exits single-connection mode into normal live browsing around the shared connection's time.
- Load flow: resolve ID → stored connection → re-query MOTIS around the stored departure time with the stored query context → strict match on leg identity (trip IDs + planned times):
  - **Match found** → display the live connection (guaranteed identical to the stored one by the strict match).
  - **Re-query succeeds, no match** → the connection is expired: show an error state saying the shared connection no longer exists, and delete the share server-side (JSON + PNG) so the link and its preview image stop working.
  - **Re-query fails** (MOTIS unreachable, network error) → show an error, do **not** delete the share.
- Unknown or already-deleted ID → same "no longer exists" error state.

## Constraints

- Deletion happens only on a confirmed-gone re-query result, never on transient failure.
- No TTL / background cleanup for now: shares that are never opened after expiry stay on disk. Accepted; a retention policy can come with the later database migration.
- Static OG tags live in `app.html` and page-level `<svelte:head>` overrides are normally forbidden — the `/s/<id>` route is the deliberate exception: overriding the tags per share is the point. The override must stay scoped to that route.
- Map assets are unavailable during SSR (existing constraint); the share page's server side may touch only the stored share data, never map assets.
- The client-side single-connection view reuses the normal routing result rendering — no separate card implementation.
- The stored JSON must be self-sufficient for image rendering and matching; it must not depend on live pipeline artifacts (e.g. `route_color_index.json`) at render time, since those change with rebuilds.
