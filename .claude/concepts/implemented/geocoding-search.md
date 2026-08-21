# Geocoding & Search

## Problem

The routing endpoint inputs currently offer "point on map" as their only non-transit-stop way to set a From/To — no address search, no POI search, no way to know the address of a point the user clicks on the map. The map needs both directions:

- **Forward geocoding**: type a query (address, university, hospital, restaurant, etc.), pick a match, use it as an endpoint.
- **Reverse geocoding**: click on the map, get the address of the click point, use it as an endpoint.

## Requirements

### Provider and proxy

- **Provider (V1)**: Photon public API (`photon.komoot.io`). Free, no auth, autocomplete-native, OSM-based, ships reverse geocoding.
- **All requests go through a SvelteKit backend proxy endpoint**, not directly from the client to Photon. This isolates provider choice from client code so V2 (self-hosted Photon on a dedicated backend host) requires no client changes.
- The proxy sets the outgoing user-agent per Photon's TOS and adds no auth. It is stateless — no cache, no persistent storage of results.
- The proxy exposes two operations (forward search, reverse lookup). Whether as two endpoints or one with a mode param is an implementation choice — the client-facing contract is what matters and stays constant across provider swaps.

### Forward search (autocomplete)

- Fires from **≥2 characters** of input. Below that, no request.
- Uses the **CH+neighbours bbox as a hard filter** (same bbox as the OSM pipeline — CH, LI, plus border margins into DE, FR, IT, AT). No `location_bias_scale` — Photon's soft bias over-biases toward the anchor for queries that explicitly name another city (e.g. "Bahnhofstrasse 10 Zürich" biased to Bern returned Ostermundigen buildings first).
- Returns up to **8 results**.
- **Includes both addresses and POIs** in one result list — no separate categories. POIs and addresses render differently (see Display format) but the user picks from a single list.
- No `lang` parameter (see Language).

### Reverse geocoding (map click)

- Triggered by clicking on the map while a routing endpoint is being set via pin.
- The endpoint is set only once the reverse lookup has returned (name attached in the same write), so routing fires a single query. A 2 s timeout falls back to a nameless coordinate endpoint.
- Queries Photon's reverse endpoint with `lon`/`lat` only. No `lang`.
- **Never resolves to a POI name**, even if the top result is a POI (`osm_key` = `amenity`, `shop`, `tourism`, `leisure`, …). "Rather no POI than the wrong POI." If the top result is a POI but carries a street, use only its street/city context (drop the POI name). If it carries no street either, treat it like the no-address fallback.
- **Fallback when no address is available at the click point** (e.g. middle of a lake, forest, rural coordinate with no addressed feature nearby): use the nearest named feature Photon returns, prefixed with a "near" descriptor and **without** any house number. Format: `Nähe [feature name], [city]`. The prefix is in the app's UI language (currently German: "Nähe"); when app-wide i18n is introduced, the prefix follows the UI locale.

### Display format

Postal codes are always omitted. Country is always omitted. House numbers appear only when present.

- **POI selection** (forward search only): `[POI name], [city]` — e.g. "Universität Bern, Bern".
- **Address selection** (forward search or reverse geocoding): `[street] [housenumber?], [city]` — e.g. "Bahnhofstrasse 10, Zürich" or "Rue du Grand-Pont, Lausanne".
- **Reverse fallback** (no address at click point): `Nähe [name], [city]` — house number never included, even if the nearest feature has one.

### Language

- **No `lang` parameter is sent to Photon.** Photon then returns OSM's base `name` tag, which by OSM convention is the place's local-language name. Passing any `lang` value forces translation or transliteration of city names to that language's exonym, which is wrong for this app. Verified against cities with distinct exonyms: Neuchâtel with `lang=de` becomes "Neuenburg" but with no `lang` stays "Neuchâtel"; Zürich with `lang=en` becomes "Zurich" but with no `lang` keeps its umlaut.
- The `country` field is a slash-concatenated mess without `lang` ("Schweiz/Suisse/Svizzera/Svizra"). Since Display format never shows country, this is not surfaced.

### Rate limiting and request coalescing

The client-side geocoding scheduler enforces:

- **Minimum 100 ms between requests.**
- **At most one request in flight at any time.**
- **Single-slot pending queue**: while a request is in flight (or during the 100 ms cooldown), new input replaces the pending query rather than queueing behind it. When the current request finishes and the cooldown has elapsed, the pending query — if still present — fires. Any intermediate queries between "the last fired one" and "the currently pending one" are dropped.
- If the input matches the last completed query verbatim, no new request is fired.

This applies to forward search only. Reverse geocoding fires once per map click, no coalescing.

### URL persistence

- For every routing endpoint (from, to, and vias), both the **coordinate** and the **display name** are written to the URL.
- On page load / refresh, input fields re-populate from the URL's display name — no geocoding request is issued to reconstruct labels.
- If an endpoint was ever set with no display name available (last-resort case where even the reverse-fallback produced nothing), the input shows coordinates as text.

## Constraints

- Reverse geocoding must **never** display a POI name, no matter how high-ranked.
- Search results must be **strictly bounded to the CH+neighbours bbox** — no results outside it.
- Photon matches on the OSM `name` field only, not name+category. Queries like "restaurant kronenhalle" return zero results; the user must type just "kronenhalle". This is a Photon limitation; document it in the UI copy if it surfaces as a user pain point. Do not try to strip category prefixes client-side — the mapping is fragile across languages and hides the real behaviour.
- No persistent storage of geocoding results anywhere (Photon TOS; also aligns with future Stadia free-tier constraints if a provider swap ever goes that way).
- The rate-limit queue is exactly one slot deep — replaces do not stack. Two rapid keystrokes while a request is in flight must not produce two follow-up requests.
- Provider swaps must not require client code changes. The proxy's request/response shape is the contract; it may translate provider quirks internally.
