# CountVision desktop app (CountVision Local)

One program for the shop PC. It starts every part of CountVision Local in the background and shows
CountVision in **its own window** (no browser, no address bar, no black command windows):

| Part | What it is | Log file |
|---|---|---|
| Database | PostgreSQL 16 (bundled, port 5544, this PC only) or an existing database | `logs\database.log`, `logs\postgres-server.log` |
| CountVision service | the API in local mode (also runs the timers for alerts and reports) | `logs\api.log` |
| App | the web app the window shows (port 3000) | `logs\web.log` |
| Counting service | the edge agent: cameras, counting, live picture (starts after the setup wizard) | `logs\edge.log` |

* Crashed parts start again by themselves (after 2, 5, 10, 30, 60 s).
* Closing the window does **not** stop counting: CountVision keeps running next to the clock (tray
  icon). Right-click the icon: Open CountVision, Allow phones and other PCs in this network, Open the
  log folder, Quit CountVision.
* Starting it a second time brings the open window to the front.
* Quit stops everything cleanly: the counting service saves its last minute, the database stops last.
* Data: `C:\ProgramData\CountVision` (`data\`, `db\`, `logs\`, `desktop.json`).

Shops install it with **one setup.exe** (Python, Node.js, PostgreSQL and the model inside, desktop
icon, autostart at logon): see [`packaging/windows/README.md`](../packaging/windows/README.md). For
development it also runs **from the source code** (below).

Installed (`C:\Program Files\CountVision\CountVision.exe`):

* The autostart at logon runs `CountVision.exe --hidden`: only the tray icon, counting starts by
  itself. A problem at start opens the window so it is seen.
* After an update the first start makes a backup (`data\backups\before-update-....cvbackup`)
  before it updates the database (`desktop.json` remembers the version that last started:
  `app_version`). A failed backup stops the start with a clear message (nothing is changed).
* Choices saved with `--remember` while testing from the source code (e.g. the Docker database) are
  ignored by the installed program: it always uses its own PostgreSQL and `C:\ProgramData\CountVision\data`.
* Logs: tray icon -> Open the log folder (`C:\ProgramData\CountVision\logs`).

## Run it from the source code on Windows

You need the repo with its `.venv`, Node.js, and a database. The easiest database is the Docker one of
the earlier tests (`docker compose up -d db` in `cloud\`, port 5433, database `countvision_local`).

```bat
cd C:\Users\hotak\Desktop\projects\countvision
.venv\Scripts\pip install -e "edge[onnx]" -e cloud\api -e desktop
.venv\Scripts\pip install pywebview pystray pillow
cd cloud\web
npm run build
cd ..\..
.venv\Scripts\countvision-desktop --data-dir C:\CountVisionLocal --database-url postgresql+psycopg://countvision:countvision@127.0.0.1:5433/countvision_local --remember --check
.venv\Scripts\countvision-desktop
```

* `--check` shows what was found and what is missing; it starts nothing. `Ready to start.` = good.
* `--remember` saves the data folder and the database, so the next start needs no arguments. Then
  `.venv\Scripts\countvision-app.exe` (no console window) is the program for a desktop shortcut.
* If `pip install pywebview` fails (its Windows part, pythonnet, may not support the newest Python
  yet), CountVision opens in **Microsoft Edge app mode** instead: also its own window without an
  address bar. Nothing else changes.
* The detector model is found in `models\`, `eval\models\` or `edge\models\`. Missing? Run
  `.venv\Scripts\countvision-edge models download --dir models` once.

Other commands:

```bat
.venv\Scripts\countvision-desktop --no-window     REM no window: progress and address in this window, Ctrl+C stops
.venv\Scripts\countvision-desktop --quit          REM stop a running CountVision cleanly
.venv\Scripts\countvision-desktop --hidden        REM start with the tray icon only (like the autostart)
```

With the bundled PostgreSQL instead of Docker (portable binaries from the PostgreSQL website, folder
with `pg_ctl.exe`): `--pg-bin C:\pgsql\bin` instead of `--database-url`. The first start creates the
database in `C:\ProgramData\CountVision\db` (user `countvision`, random password in `db-password`).

## Phones and other PCs

Off by default: only this PC can open CountVision. Tray icon -> "Allow phones and other PCs in this
network": the app then listens on the network (Windows asks once whether to allow it) and
invitation links use this PC's name, e.g. `http://SHOP-PC:3000`. The API itself always stays on
`127.0.0.1`; other devices only reach the web app.

## Privacy

The live camera picture comes from the counting service on this PC (`127.0.0.1:8002`, key
protected) through the API, only to people logged in to this business. The owner chooses in
Settings -> Privacy: people blurred (default), full picture, or off. Nothing is recorded. Next.js
telemetry is switched off (`NEXT_TELEMETRY_DISABLED=1`), ONNX Runtime telemetry too (edge agent).

## Tests

```bash
pip install -e "desktop[dev]"
pytest desktop/tests      # launcher, background programs, one instance, window logic (fake pywebview)
```

The `local` job in `.github/workflows/ci.yml` runs the real launcher (`--no-window`, its own
PostgreSQL) and the browser test of a fresh installation against it; `desktop-windows` runs the
launcher tests and the live picture tests on Windows.

## Licences

pywebview: BSD-3-Clause. pystray: LGPL-3.0, used unmodified as a separate library (the installer keeps
it as a normal Python package that can be replaced, which is what the LGPL asks for). Pillow: HPND
(permissive). Microsoft Edge WebView2: part of Windows. PostgreSQL: PostgreSQL licence (permissive).
Node.js: MIT.
