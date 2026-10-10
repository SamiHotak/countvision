# CountVision

CountVision turns a shop's existing cameras into visitor and traffic numbers.

- A small program on site (the **edge agent**) watches the camera, detects and tracks people and vehicles, and counts them.
- **Only numbers leave the building.** No video, no pictures, no faces are stored or sent.
- Counts are saved in a local SQLite file, in 1-minute steps.

## Start (Windows, one command)

```powershell
.\start.bat            # your webcam, with the config edge\configs\local.yaml
.\start.bat --demo     # demo video: no camera, no model
.\start.bat --phone    # also open it on your phone in the same Wi-Fi (people blurred)
.\start.bat --count    # counting only, all cameras (for the pilot PC)
```

The first start creates `.venv` and installs everything. Then the browser opens
`http://127.0.0.1:8000`: live picture, draw lines and zones with the mouse, big live counters,
today's chart and CSV download.

## Status

- Phase 1, Session A: the edge agent (`edge/`): inputs, detectors, tracking, lines, zones, SQLite.
- Phase 1, Session B: the local web app (`countvision-edge app`).
- Phase 2, Session A: evaluation on hand-labeled clips, speed benchmark, YOLOX (Apache-2.0)
  detector. Results and recommendation: [`eval/results.md`](eval/results.md).
- Phase 2, Session B: several cameras per device, Docker images, one-line installers,
  privacy defaults, pilot pack ([`docs/pilot/`](docs/pilot/README.md)).
- Phase 3, Session A: the cloud skeleton ([`cloud/`](cloud/README.md)): accounts (email + Google),
  organizations, roles, invitations, activity log, background emails. Local start:
  `cd cloud`, `copy .env.example .env`, `docker compose up -d --build`, open http://localhost:3000.
- Phase 3, Session B: sites, devices with one-time pairing codes, the edge agent uploads its numbers
  (and replays them after a network outage), device status page with today's counts.
- Phase 3, Session C: draw lines and zones in the browser on a pixelated snapshot, choose what to
  count and the counting hours; the device uses them within about a second. Live counters (SSE).

## Install on a pilot device (one line)

```powershell
# Windows 10/11 (PowerShell, not as admin)
irm https://raw.githubusercontent.com/SamiHotak/countvision/main/install.ps1 | iex
```

```bash
# Linux mini-PC (Ubuntu/Debian, Docker)
curl -fsSL https://raw.githubusercontent.com/SamiHotak/countvision/main/install.sh | sudo bash
```

Docker by hand: [`docker/README.md`](docker/README.md).

## Folders

| Folder | What it is |
|---|---|
| `edge/` | The edge agent (Python). See `edge/README.md`. |
| `edge/countvision_edge/app/` | The local web app (FastAPI + one plain HTML/JS page). |
| `start.bat` | Windows: install on first start, then open the local app (`--count`: counting only). |
| `install.ps1`, `install.sh` | One-line installers (Windows / Linux with Docker). |
| `docker/` | Docker images (CPU, NVIDIA, Jetson), compose file, test RTSP cameras. |
| `cloud/` | The SaaS app: `api/` (FastAPI, Postgres/TimescaleDB, Redis, Celery) and `web/` (Next.js). See `cloud/README.md`. |
| `docs/pilot/` | Pilot pack: install guide, sign, privacy notice, agreement, AVV, DPIA, checklist. |
| `eval/` | Hand labels, accuracy and speed results, `laptop.bat`, Colab notebook. See `eval/README.md`. |
| `.github/workflows/` | CI: lint, tests and browser tests on every push (edge, cloud, Docker; CPU only). |

## Licence

AGPL-3.0-or-later (see `LICENSE`). The Ultralytics YOLO models are AGPL too. For a closed-source product you need a permissive model (YOLOX or RF-DETR, Apache-2.0) or an Ultralytics Enterprise licence. The detector is swappable on purpose.

Evaluation videos: Intel IoT DevKit sample videos (CC BY 4.0). YOLOX models: Megvii (Apache-2.0).
