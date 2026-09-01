# Account System

## Problem

Kora Maps has no user accounts. Upcoming features need them: per-user
preferences, saved routing links, most-frequented stations (touch
timetable), statistics, and later ticketing. This introduces the app's
first database. The auth system is ported from the Ogoy project (the
more evolved of the user's two existing login systems), with the
email-confirmation policy of steppenwolf-x and a set of agreed fixes.

## Requirements

### Database

- PostgreSQL, accessed via Prisma (`provider = "postgresql"`). Chosen
  for the long term: JSONB for future document-ish data (stored
  routes), PostGIS as a spatial option, ACID for future ticketing. No
  second database (no MongoDB).
- Runs on the production VPS alongside the existing MariaDB, tuned
  lean (small `shared_buffers`, low `max_connections`).
- New env var `DATABASE_URL` in `.env` / the `ENV_VARS` repo secret.

### Data model (initial)

- `User` — id, email (unique), passwordHash, emailConfirmed,
  createdAt, language. Minimal; feature tables (preferences, saved
  links, station frequency) come with their features.
- `Session` — tokenHash (unique), expiresAt, createdAt, userId
  (cascade delete, indexed).
- `UserVerification` — tokenHash, type (`EMAIL_VERIFICATION`,
  `PASSWORD_RESET`), expiresAt, userId; unique on (userId, type) so a
  new token replaces the old one.

### Sessions

- DB-backed opaque session tokens in an `HttpOnly; Secure;
  SameSite=Lax` cookie. No JWT.
- Tokens (session and verification) stored **hashed with SHA-256** at
  rest — never plaintext, never bcrypt (bcrypt on per-request lookups
  is the performance trap from the first Ogoy attempt).
- **Working sliding renewal**: a session presented within its last day
  of validity is extended and the refreshed cookie actually written
  (fixes the dead-code renewal branch present in both source projects).
- Expired sessions removed by a scheduled cleanup, not only
  opportunistically on presentation.

### Auth flows

JSON endpoints (no form actions), under the app's `/api/` namespace:
register, login, logout, status, change-pw, forgot-pw, reset-pw,
resend-confirmation, delete-user.

- Registration and password reset log the user in directly by creating
  the session in-process — no internal HTTP round-trip to the login
  endpoint.
- forgot-pw always returns success (no email enumeration).
- Password hashing: bcryptjs, cost 10 (as in Ogoy).
- **Rate limiting** on login, register, forgot-pw, and
  resend-confirmation: in-memory per-IP/per-email throttle (the app is
  single-instance; no external store needed).
- Registration spam protection: honeypot field as in Ogoy; Turnstile
  only if abuse appears.

### Email confirmation policy

- **Confirmation is NOT required for basic account use**
  (steppenwolf-x policy, Ogoy mechanism): the auth guard's default
  requirement is "authenticated"; endpoints needing a confirmed email
  opt in explicitly, and the check is enforced **server-side**, not
  only in the UI.
- Confirmation mail sent on registration; verification link route
  consumes the token and sets `emailConfirmed`.
- Mail transport: nodemailer + AWS SES as in Ogoy, including the
  SES/SNS webhook endpoint feeding a bounce/complaint suppression
  check before every send.
- No auto-deletion of unconfirmed accounts (they are fully usable).

### Guards and state

- No AsyncLocalStorage `userState` proxy (an Ogoy pattern Kora
  doesn't need): server code reads the auth info explicitly from
  `locals`, the client keeps a plain reactive user-state store
  populated at load / via the status endpoint.
- Server enforcement is per-endpoint via `checkAuth(required)`
  throwing 401/403; route groups stay organizational only.
- CSRF: SvelteKit's built-in origin check cannot exempt single routes,
  and the SES/SNS webhook (which arrives with mailing) needs one — so
  the built-in check is disabled and replaced by an equivalent custom
  origin check in hooks that exempts only the webhook route. Never a
  blanket `trustedOrigins: ['*']` with no replacement check.

## Constraints

- The existing `/stats` basic-auth guard in `hooks.server.ts` must
  keep working; the auth handle sequences with it.
- No feature currently requires a confirmed email — the gate mechanism
  ships, but which endpoints use it is decided per future feature.
- No OAuth2 provider, no Stripe/subscription machinery, no
  `DeletedUser` tombstone — Ogoy features Kora doesn't need yet.
- Known Ogoy quirks are not ported: the `veryfyToken` typo, the
  internal-login round-trip, plaintext tokens, the dead renewal
  branch, `trustedOrigins: ['*']`.
