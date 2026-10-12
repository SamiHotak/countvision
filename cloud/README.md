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

## Alerts, API, webhooks, video analysis (Phase 4 Session B)

**Alerts** page (`/app/orgs/<org>/alerts`):
- *Where alerts go* (channels): **Telegram** (bot token from @BotFather, then "Find my chat"), **email**
  (members with a confirmed address), **webhook** (signed POST). **Send test** tries the channel at once and
  shows Telegram's / the webhook's answer. Telegram tokens and webhook secrets are stored encrypted
  (key derived from `CV_SECRET_KEY`: if you change that key, set the channels up again).
- *Alerts* (rules), checked every minute by Celery beat:
  - **Too many people**: people in area zones >= limit in each of the last N minutes;
  - **Long queue**: people waiting in queue zones >= limit for N minutes;
  - **Camera not counting**: a camera or its computer does not count for N minutes (not checked
    outside the camera's counting hours);
  - **Fewer visitors than usual**: visitors in the last N minutes (>= 30) below X % of the same weekday
    and time in the last 4 weeks (needs 2 weeks where the cameras ran and >= 10 normal visitors).
  One message when it starts, one when it is over (2 minutes below the limit: no flapping). Minute
  numbers arrive 1-2 minutes late, so an alert comes 1-3 minutes after the moment.
- *History*: started / ended, kept 400 days.

**Telegram in 2 minutes**: Telegram → @BotFather → `/newbot` → copy the token → open your bot and send
"hi" → CountVision Alerts → Add channel → Telegram → paste token → Find my chat → Add channel → Send test.

**API** page: API keys (admins; shown once, stored hashed, revoke any time), curl examples, how to check
webhook signatures. The public API is read-only at `/api/v1` with its own docs at **/api/v1/docs**:
`GET /sites`, `/sites/{id}/live`, `/sites/{id}/counts?from=&to=&bucket=minute|hour|day&by=total|line|class`,
`/sites/{id}/zones`, `/cameras`, `/alerts`. Header `Authorization: Bearer cvk_...`, 120 requests/min per key.

**Webhooks**: JSON `{"id","type","created_at","org_id","data"}`, types `alert.fired`, `alert.resolved`,
`crossing` (every line crossing, batches of up to 500, within seconds), `counts.hourly` (per site, ~5 min
after each local hour), `test`. Header `X-CountVision-Signature: t=<unix>,v1=<HMAC-SHA256(secret, "t.body")>`.
Failed requests are retried after 1, 5, 30, 120, 360 minutes (same `id`), then "failed"; the API page
shows the last deliveries with a Retry button. Production refuses webhook addresses in private networks.

**Video analysis** (`/app/orgs/<org>/video`): upload a video (MP4, MOV, AVI, MKV …, max 2 GB / 2 hours; in
8 MB pieces, a broken upload continues where it stopped) → the worker reads it and shows a frame →
draw lines/zones like in the camera editor, choose classes, 10 FPS or every frame, annotated video with
people pixelated → **Start counting** → progress live → IN/OUT per line and class, zones, visit lengths,
counts over the video, annotated H.264 video in the page, PDF and CSV. It uses the SAME counting engine as
the edge agent (`countvision_edge`) with YOLOX-Tiny (Apache-2.0).
- Privacy: the uploaded video is **deleted when the analysis ends** (failed/stopped: after 1 day,
  never started: after 7 days); the annotated video after `CV_VIDEO_KEEP_DAYS` (7); numbers stay until
  the job is deleted. The customer needs a legal basis for the recording (the page says so).
- Docker: the **video-worker** container (Celery queue `video`, one video at a time) and the `jobs-data`
  volume. Without Docker: `CV_VIDEO_RUNNER=thread` runs it inside the API process.
- Measured in the sandbox (2 CPU cores, CPU only): 768x432 clip with YOLOX-Tiny incl. H.264 encoding
  at 2.1x real time; the labelled `one-by-one-person-detection` clip: 6/6 crossings right.

The image is bigger now (OpenCV, ONNX Runtime, ffmpeg, the model: about 1.2 GB) and is built from the
repository root (`cloud/api/Dockerfile`, allow list in `cloud/api/Dockerfile.dockerignore`).

## Dashboards and reports (Phase 4 Session A)

**Overview** (`/app/orgs/<org>`): every site with visitors today, the change vs. the same time last
week, people inside / waiting now and a small 24-hour chart.

**Site dashboard** (Sites → **Dashboard**, `/app/orgs/<org>/sites/<site>`): period Today / Yesterday /
7 days / 30 days / any dates (up to 366 days; the period is in the address, so a link can be shared).
- Visitors in (big number) and the change vs. last week (up to 7 days) or the previous period of the
  same length. A running day is compared with the **same time** last week. When the cameras ran
  less than half as long in the comparison period (for example the first weeks after installation)
  there is **no** percentage: "+467 %" would only measure missing data.
- Inside now, in the queue now, went out, busiest hour, average visit, data coverage.
- Visitors per hour (1-2 days) or per day, with last week as a thin line; hover or arrow keys show the
  values; **Show table** shows the same numbers as a table.
- Lines with IN/OUT. **Counts as visitors**: tick only the door lines, not aisle lines (members).
- What was counted (people, bicycles, ...), length of visits (zone visits), weekday x hour pattern
  (average per hour over 4 weeks; only hours when the cameras ran count), each zone over time,
  and how complete the data is (analysed time vs. counting hours).
- Live: a crossing on one of the site's cameras updates the page within ~2 s.
- Downloads: **PDF report**, **CSV lines** (in/out per hour or day, per class), **CSV zones**. CSV
  uses ";" and decimal commas when the browser writes numbers German style (Excel), "," otherwise.

Where the numbers come from: finished minutes from the 1-minute rows, the newest seconds from the
single crossing events (the border is the newest analysed minute of each camera), so the dashboard
is exact and live at the same time. Everything is in the site's time zone.

**Reports** page: emails a PDF report (optional CSV) to members of the organization: daily (about
yesterday), weekly on a chosen day (about the 7 days before) or monthly on the 1st (about last
month), at a chosen hour in the site's time zone. Celery beat checks every 5 minutes; each report is
sent once (also after a server pause only the newest missed one). Only members with a confirmed
email get it; one email per person. **Send now** sends the newest finished period at once (max 10 per
hour). Errors are shown on the page; after 3 failed tries that period is skipped.

**Sample data**: on an organization without devices the overview offers **Add sample data**: a site
"Demo café (sample data)" with 5 weeks of invented numbers (opening hours, a busy Saturday, a queue,
one camera outage). Remove it on its dashboard. Command line: `countvision-cloud sample-data --org <slug>`.

API added: `GET /api/orgs/<org>/dashboard`, `GET /sites/<id>/dashboard?from=&to=`,
`PUT /sites/<id>/visitor-lines`, `GET /sites/<id>/export.csv?from=&to=&kind=lines|zones&bucket=minute|hour|day&delimiter=,|;`,
`GET /sites/<id>/report.pdf?from=&to=`, `GET|POST /report-schedules`, `PATCH|DELETE /report-schedules/<id>`,
`POST /report-schedules/<id>/send`, `POST|DELETE /sample-data`. CLI: `countvision-cloud send-reports`.

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
pytest cloud/api/tests                              # API: 94 tests, real Postgres + Redis (pip install -e edge for the video tests)

# Browser tests: API with the in-memory mailbox + built web app
$env:CV_EMAIL_BACKEND="memory"; $env:CV_EMAIL_DELIVERY="sync"; $env:CV_SIGNUPS_PER_IP_HOUR="1000"
countvision-cloud serve                             # window 1
cd cloud\web; npm run build; npm start              # window 2
cd cloud\web; npx playwright install chromium; npx playwright test   # window 3
```

GitHub CI runs both (`cloud` job) and builds and smoke-tests the full Docker stack (`cloud-docker` job).

## Licences of the parts

FastAPI, Starlette, SQLAlchemy, Alembic, pydantic, uvicorn, redis-py, httpx, Celery, argon2-cffi,
Next.js, React, Tailwind, Barlow font (OFL), ReportLab (PDF, BSD), cryptography (Apache-2.0/BSD), OpenCV (Apache-2.0),
ONNX Runtime (MIT), YOLOX-Tiny (Apache-2.0): permissive (MIT / BSD / Apache / OFL).
The PDF reports embed Barlow (OFL, `product/reports/fonts/OFL.txt`); DejaVu Sans is the fallback font
for other alphabets (free licence).
psycopg: LGPL-3.0 (used as a normal library: fine for closed code too).
ffmpeg (Debian package, LGPL/GPL) runs as a separate program to write H.264 videos; it is not linked
into CountVision code. TimescaleDB community features: Timescale License (free to use in your own SaaS; you may not sell
"TimescaleDB as a service"). Valkey (Redis-compatible): BSD. Mailpit: MIT (development only).
The CountVision code itself is AGPL-3.0 for now (see the root README).
