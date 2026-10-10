# CountVision cloud

The online part of CountVision: accounts, organizations, team roles and invitations now;
sites, cameras, devices, live counters and dashboards in the next sessions.
It runs completely on your PC for 0 EUR. Nothing here costs money.

| Folder | What it is |
|---|---|
| `api/` | Backend: Python 3.12, FastAPI, SQLAlchemy 2 + Alembic, PostgreSQL (+ TimescaleDB), Redis/Valkey, Celery |
| `web/` | Web app: Next.js (App Router), TypeScript, Tailwind, shadcn/ui-style components |
| `compose.yaml` | The whole stack for local development (database, Redis, Mailpit, API, worker, beat, web) |

## Start (Windows, Docker Desktop)

Docker Desktop must show "Engine running". In PowerShell, in the repo folder:

```powershell
cd cloud
copy .env.example .env
docker compose up -d --build
```

The first start downloads and builds for 5-15 minutes. Then:

- http://localhost:3000 — the web app (sign up, create an organization, invite people)
- http://localhost:8025 — Mailpit: every email the app sends lands here (nothing really leaves your PC)
- http://localhost:8001/api/docs — the API documentation (development only)

Useful commands (in `cloud\`):

```powershell
docker compose ps                     # all services "running" / "healthy"?
docker compose logs -f api worker     # follow the logs (Ctrl+C to stop following)
docker compose down                   # stop; your data stays
docker compose down -v                # stop and DELETE all data (fresh start)
docker compose up -d --build api web  # after changing code
```

## How it fits together

```
browser ──> web (Next.js, :3000) ──/api/*──> api (FastAPI) ──> PostgreSQL + TimescaleDB
                                                  │
                                                  └──> Redis/Valkey ──> worker (Celery: emails)
                                                                         beat (timers: cleanup, heartbeat)
```

- The browser only talks to the web server. It passes `/api/*` to the API, so the login cookie is
  first-party and there is no CORS.
- Backend layers: `routers` (HTTP) -> `services` (rules: who may do what) -> `repositories` (SQL) -> `models`.
- `countvision_cloud/saas/` has the product-neutral basics (accounts, organizations, roles, invites,
  emails, audit log). It knows nothing about cameras, so it can be reused for other products.
- `countvision_cloud/product/` is CountVision itself: sites, devices (pairing, tokens, upload API),
  cameras and the counting data (TimescaleDB hypertables when the extension is there).
- Edge agents talk to `/api/device/*` with `Authorization: Bearer cvd_...` (no cookies, no CSRF header).

## Connect an edge device (Phase 3 Session B)

1. Web app: **Sites → Add site**, then **Devices → Add device**. You get a code like `K7QF-3MXP`
   (15 minutes, one use) and the exact command for the device.
2. On the device (Windows, in the countvision folder):
   ```powershell
   .venv\Scripts\countvision-edge pair --config edge\configs\local.yaml --url http://localhost:3000 --code K7QF-3MXP
   .\start.bat --count
   ```
   Edge in Docker on the same PC: see `docker/README.md` (use `http://host.docker.internal:3000`).
3. The device page shows Online, the cameras, FPS and today's IN/OUT per line (live, see below).
   Pull the network cable: the device keeps counting; after reconnecting it sends everything it missed.

Upload API (`POST /api/device/ingest`): batches of minute rows (line counts, zone stats, coverage),
events, heartbeats and camera states; up to 5000 rows per list. Minute rows are stored with
"insert or overwrite" and events once, so a repeated batch changes nothing. Rows more than 1 day in
the future or older than 400 days are rejected and counted. A device counts as offline after 90 s
without contact. Removing a device makes its token useless at once; its numbers stay.

## Camera editor and live counters (Phase 3 Session C)

Device page → camera → **Edit lines and zones** (`/app/orgs/<org>/cameras/<camera>`):

- **Take snapshot**: asks the device for ONE picture. The device pixelates every detected person and
  vehicle and scales it to max 960 px. The cloud keeps it **10 minutes in Redis memory only** (never in
  the database or on disk) and logs who asked (`camera.snapshot_requested`). Devices with
  `privacy.snapshots: false` refuse; you draw on a grid instead.
- Tools: **Select (V)**, **Line (L)** (drag), **Zone (Z)** (click corners, click the first one again /
  double-click / Enter to finish), Undo/Redo (Ctrl+Z, Ctrl+Shift+Z), Snap (S: corners and 0/45/90°),
  Flip IN direction (F), Delete. Rename in the side panel (a new name starts a new count).
- What to count (people, bicycles, cars, motorcycles, buses, trucks), count point (feet / middle),
  counting hours (days + from/to in the site's time zone; outside them the camera is "paused").
- **Save and send to device** → new version → the device, waiting in `GET /api/device/poll`, gets it
  within about a second, applies it to the running camera and reports the version. The panel shows
  "Sending…", "Version N is running on the device", or the device's error. Members and above edit,
  viewers see the lines and live counts.

Live (`GET /api/orgs/<org>/live`, Server-Sent Events): line crossings, device status, config versions
and snapshot results are pushed to open pages (only numbers and names). Devices send new crossings
within ~1 s; "today" = line-crossing events since local midnight. Measured in the sandbox: crossing on
the device → browser 0.6–0.95 s. Pages still refresh slowly as a fallback. A reverse proxy in front
(Phase 5, Caddy) must not buffer `text/event-stream`.

Device API added: `GET /api/device/poll?rev=&wait=` (long-poll, max 55 s, holds no database
connection while waiting), `POST /api/device/snapshots/<request>` (JPEG body or `{"error": ...}`).
Org API added: `GET /cameras/<id>`, `PUT /cameras/<id>/config`, `POST /cameras/<id>/snapshot`,
`GET /cameras/<id>/snapshot/<request>` (+ `/image`).

## What works (Phase 3 Sessions A and B)

- Sites (name, address, time zone), devices with one-time pairing codes, device tokens (stored hashed),
  automatic cameras, upload API, device page with live state and today's counts, remove / delete devices.

- Sign up with email + password, confirm the email by link, log in/out, "forgot password" by email,
  change password, log out other devices, delete account.
- "Continue with Google" (only shown when you set the two Google values, see below).
- Organizations with roles **owner > admin > member > viewer**. Admins manage members and invitations,
  only owners manage owners and can delete the organization. The last owner cannot leave or be removed.
- Invitations by email (7 days, one-time link, only the invited address can use it; sending again
  makes a new link). New people sign up directly from the invitation.
- Activity log per organization (who invited, removed, changed roles ...). No IP addresses stored.
- Background worker sends the emails; hourly cleanup deletes expired logins, links and invitations.

## Security and privacy choices

- Passwords: Argon2id. Login cookie: random 256-bit token, only its SHA-256 is stored; `HttpOnly`,
  `SameSite=Lax`, `Secure` on HTTPS; new token at every login; 30 days, extended while you use it.
- Every change (POST/PATCH/DELETE) needs the header `X-CountVision: 1` (CSRF protection).
- Rate limits: 10 wrong passwords per email in 15 minutes, 3 reset emails per hour per address,
  20 sign-ups per IP per hour, 30 invitations per user per hour.
- Login and "forgot password" answer the same for unknown emails (nobody can test which emails exist).
- Emails are sent only after the database saved the change (no mails with dead links).
- Google: account linking to an UNCONFIRMED password account removes that password, so nobody can
  register your email before you and keep access.
- Web app fonts are self-hosted (no Google Fonts request). No tracking, no analytics.

## Google login (optional, free)

1. https://console.cloud.google.com -> create a project -> "APIs & Services" -> "OAuth consent screen":
   External, app name CountVision, your email. Add yourself as a test user.
2. "Credentials" -> "Create credentials" -> "OAuth client ID" -> "Web application".
   Authorized redirect URI: `http://localhost:3000/api/auth/google/callback`
3. Copy the client ID and secret into `cloud\.env`:
   ```
   CV_GOOGLE_CLIENT_ID=....apps.googleusercontent.com
   CV_GOOGLE_CLIENT_SECRET=....
   ```
4. `docker compose up -d` (restarts the API with the new values). The login page now shows the button.

## Develop without Docker (optional)

Needs Python 3.12+, Node 22, and Postgres + Redis (easiest: `docker compose up -d db redis mailpit`).

```powershell
# API (port 8001), from the repo root
python -m venv .venv-cloud
.venv-cloud\Scripts\Activate.ps1
pip install -e "cloud/api[dev]"
$env:CV_EMAIL_BACKEND="smtp"; $env:CV_EMAIL_DELIVERY="sync"
countvision-cloud migrate
countvision-cloud serve --reload

# Web app (port 3000), second window
cd cloud\web
npm install
npm run dev
```

## Tests

```powershell
docker compose up -d db redis                       # in cloud\
pytest cloud/api/tests                              # API: 51 tests, real Postgres + Redis

# Browser tests: API with the in-memory mailbox + built web app
$env:CV_EMAIL_BACKEND="memory"; $env:CV_EMAIL_DELIVERY="sync"; $env:CV_SIGNUPS_PER_IP_HOUR="1000"
countvision-cloud serve                             # window 1
cd cloud\web; npm run build; npm start              # window 2
cd cloud\web; npx playwright install chromium; npx playwright test   # window 3
```

GitHub CI runs both (`cloud` job) and builds and smoke-tests the full Docker stack (`cloud-docker` job).

## Licences of the parts

FastAPI, Starlette, SQLAlchemy, Alembic, pydantic, uvicorn, redis-py, httpx, Celery, argon2-cffi,
Next.js, React, Tailwind, Barlow font (OFL): permissive (MIT / BSD / Apache / OFL).
psycopg: LGPL-3.0 (used as a normal library: fine for closed code too).
TimescaleDB community features: Timescale License (free to use in your own SaaS; you may not sell
"TimescaleDB as a service"). Valkey (Redis-compatible): BSD. Mailpit: MIT (development only).
The CountVision code itself is AGPL-3.0 for now (see the root README).
