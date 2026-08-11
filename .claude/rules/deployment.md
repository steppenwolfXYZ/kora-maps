# Deployment

Two deliberately separate deploy channels:

1. **App** (SvelteKit build) — automatic, GitHub Actions on every push to `main`.
2. **Map assets** (pmtiles, style.json, indexes, glyph fonts) — manual, `scripts/deploy_map_assets.sh`, run only when a pipeline result is worth publishing. Not integrated into the pipeline on purpose: not every rebuild produces a publishable outcome.

The split exists because map assets are large generated artifacts (~470 MB, gitignored) while the app is small committed code. It also maps cleanly onto the future setup where a dedicated pipeline server runs nightly rebuilds and pushes assets itself — the GitHub Actions side never changes.

## Production environment

- **Server:** shared Hetzner VPS (Debian), `91.99.74.183` / `2a01:4f8:c0c:cbf0::1`, hosts other low-traffic sites too. Nothing heavy may run there — the transit pipeline never runs on this machine.
- **Domain:** `koramaps.app` + `www.koramaps.app` (A + AAAA on both).
- **Deploy user:** `ga_koramaps`, owns `/var/www/koramaps.app/`. GitHub Actions authenticates with the repo secret `SSH_PRIVATE_KEY`; the local machine uses the `~/.ssh/config` alias `koramaps` (same user, personal key).
- **Directory layout** under `/var/www/koramaps.app/`:
  - `app/` — live app (build + node_modules + ecosystem.config.cjs + .env). Overwritten by every app deploy.
  - `build-environment/` — staging dir the workflow rsyncs into; removed after each finalize.
  - `map-assets/` — map data, written only by `deploy_map_assets.sh`.
- **Process manager:** pm2, app name `koramaps`, defined in `ecosystem.config.cjs` (repo root, deployed with the artifact). It runs `build/index.js` (adapter-node) with `node --env-file=.env` — requires node ≥ 20.6 on the server. All runtime config (`PORT=3012`) lives in `.env`, which the workflow writes from the `ENV_VARS` repo secret. No ORIGIN var: the planned login system is JSON/REST, which bypasses SvelteKit's form-action CSRF path.
- **nginx:** site file `/etc/nginx/sites-available/koramaps.app`. `location /map-assets/` is an alias to the map-assets dir — nginx serves pmtiles directly (range requests, 1 h cache header); the node app never sees those requests. `location /` proxies to `localhost:3012`. TLS via certbot (`--nginx -d koramaps.app -d www.koramaps.app`); the port-80 server block is required (https redirect + ACME renewals) — do not "clean it up".

## App deploy (`.github/workflows/deploy.yml`)

Mirrors the user's standard workflow used across their projects (same shape as ogoy.app; keep them consistent): build on the GA runner (never on the VPS), `npm prune --omit=dev`, assemble `deploy_artifact/` (build, node_modules, package.json, ecosystem.config.cjs, .env), rsync to the server staging dir, then a server-side finalize: rsync staging → `app/`, delete staging, `pm2 startOrRestart ecosystem.config.cjs`. Repo secrets: `ENV_VARS` (content of .env) and `SSH_PRIVATE_KEY`.

adapter-node does not bundle production dependencies — that is why node_modules ships in the artifact.

## Map assets deploy (`scripts/deploy_map_assets.sh`)

Rsyncs `static/map-assets/` → `map-assets/` on the server over the `koramaps` SSH alias. Allowlist: `*.json` (style, stop-search index, line index), `fonts/` (MapLibre glyph PBFs), `tl_*.pmtiles`; excludes `tl_debug_*` and anything else (stale/legacy files never leave the machine). `--delete` inside the target dir. Extra args pass through to rsync (`--dry-run`). Fonts transfer once; later runs skip them as unchanged.

Run it before the first app deploy on a fresh server — without assets the app serves but the map cannot load.

## SSR constraints (deployment-driven)

Map assets never exist inside the server build, so the app must not touch them during SSR:

- `style.json` is fetched client-side in `+page.svelte` (`onMount`), never in a load function — a server-side fetch would 404 in production. app.html carries a `<link rel="preload">` so the download still starts with the document.
- The style object is held in `$state.raw` — Map.svelte's init effect mutates `style.layers` in place, and a deeply reactive proxy would make that effect re-trigger itself in an endless map-recreate loop.
- All URL writes go through SvelteKit's `replaceState` / `pushState` (`$app/navigation`), never raw `history.replaceState`. MapLibre's `hash: true` is NOT used — Map.svelte has its own position-hash sync (same `#zoom/lat/lng` format, written on `moveend`). Opening the line detail view is the one write that pushes rather than replaces, so browser back closes it; its close button correspondingly calls `history.back()` to consume that entry (see `line-detail-view.md` § Deep link). The selection rides along in SvelteKit's `page.state` (typed in `src/app.d.ts`), so the position-hash writer must preserve that state instead of overwriting it with an empty object.

## UI fonts (self-hosted, no Google CDN)

`static/fonts/` (committed): `saira-vf-latin.woff2` + `saira-vf-latin-ext.woff2` (variable, weights 100–900, covers UI + splash) and `material-symbols-subset.woff2` (icon font subsetted to the six mode icons used by StopSearch). `@font-face` rules live inline in `app.html`. These are separate from `map-assets/fonts/` (MapLibre SDF glyph PBFs for tile labels) — both are needed; MapLibre cannot use web fonts.

If a new icon is added to `MODE_ICON` in StopSearch.svelte, regenerate the subset: fetch `https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20,400,0,0&icon_names=<comma-separated, alphabetical, incl. new icon>&display=block` with a browser user agent, download the woff2 URL it contains, replace `material-symbols-subset.woff2`.

## Ops notes

- First-line diagnostics: `ssh koramaps`, then `pm2 list`, `pm2 logs koramaps`.
- After a first-ever pm2 start: `pm2 save` so the app survives server reboots.
- Cert renewal is automatic (certbot timer). Never run bare `certbot --nginx` (interactive all-domains checklist) or `certbot delete` on this shared server; always scope with explicit `-d`.
