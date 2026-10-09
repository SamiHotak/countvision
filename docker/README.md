# CountVision in Docker

One container counts all cameras of a site. It restarts itself after a crash or a reboot,
reconnects lost cameras and reports its health to Docker. Only numbers are stored.

| File | What |
|---|---|
| `Dockerfile` | CPU image (any x86-64 or ARM64 PC). ONNX Runtime + YOLOX-Tiny (Apache-2.0). **Tested.** |
| `Dockerfile.nvidia` | NVIDIA GPU image (CUDA 12). Install steps tested, **not yet run on a GPU**. |
| `Dockerfile.jetson` | NVIDIA Jetson (JetPack 6). **Not tested** (no Jetson). |
| `compose.yaml` | The services: `countvision` (counting), `setup` (web app), `countvision-gpu`, `test-camera` |
| `.env.example` | Camera addresses and passwords. Copy to `.env`. |
| `test-camera/` | Two fake IP cameras that loop the evaluation videos, plus a matching config |
| `entrypoint.sh` | `run` (default), `app`, `health`, or any `countvision-edge` command |

Data on the host: `config/config.yaml` (your settings, created on first start) and `data/`
(database, `status.json`). Both are next to `compose.yaml` and are not in git.

## Windows laptop (Docker Desktop): first test with the test cameras

Docker Desktop is free for personal use and small businesses (fewer than 250 employees and
less than $10 million revenue). Docker Desktop **cannot** use a USB webcam; use the test cameras
or a real IP camera.

```powershell
cd C:\Users\hotak\Desktop\projects\countvision
.venv\Scripts\activate.bat
countvision-edge eval fetch           # the test videos (once)
cd docker
copy .env.example .env
docker compose --profile test up -d --build
```

The first build takes 3–10 minutes. Then:

```powershell
docker compose ps                     # countvision: "Up ... (healthy)" after 1-2 minutes
docker compose logs -f countvision    # heartbeats every 30 s, Ctrl+C to leave
docker compose exec countvision countvision-entrypoint health
docker compose exec countvision countvision-edge report --db /data/countvision.db
```

**Test the reconnect:** `docker compose stop test-camera`, wait 1 minute (health shows
`offline`), `docker compose start test-camera`. Within ~15–30 s both cameras are `running` again.

Stop everything: `docker compose --profile test down` (the numbers in `data\` stay).

## Real cameras

1. Find the RTSP address of each camera (manual or app: "RTSP", "ONVIF"). Use the **sub stream**
   (640×360 or 1280×720), not 4K. Test it first in VLC: Media → Open Network Stream.
2. Edit `.env`: delete the 3 test lines, add `CAM1_URL=rtsp://user:password@192.168.1.20:554/...`
3. Start once: `docker compose up -d --build`. It creates `config/config.yaml` from
   `edge/configs/site.example.yaml`. Edit it: one block per camera (`uri: ${CAM1_URL}` ...).
4. Draw the lines in the web app (counting must be stopped, or everything counts twice):

   ```powershell
   docker compose stop countvision
   docker compose --profile setup run --rm --service-ports setup app --camera entrance
   ```

   Open `http://<this PC's IP>:8000/?key=...` (the key is printed). People are pixelated.
   Draw, **Save and count**, then Ctrl+C and `docker compose start countvision`.

## Connect to the CountVision cloud

1. In the web app: **Devices → Add device**, choose the site, give the device a name. You get a code.
2. In this `docker` folder (the cloud on the same PC: use `host.docker.internal` instead of `localhost`):

   ```powershell
   docker compose run --rm countvision pair --url http://host.docker.internal:3000 --code XXXX-XXXX
   docker compose restart countvision
   ```

3. Within 15 s the device shows **Online** in the web app, its cameras appear after the first upload.
   Check here: `docker compose exec countvision countvision-edge cloud-status`.

Linux installer: `sudo countvision pair --url https://<cloud> --code XXXX-XXXX` (restarts by itself).

## Linux mini-PC

Use the one-line installer (`install.sh` in the repo root): it installs Docker if needed, sets
up `/opt/countvision` and starts the service. Manual way: same as above, plus once
`sudo chown -R 10001 config data` (the container runs as user 10001, not as root).
USB webcam: uncomment `devices: - /dev/video0:/dev/video0` in `compose.yaml`.

## NVIDIA GPU

Needs the NVIDIA driver and the NVIDIA Container Toolkit on a Linux host. Then use
`countvision-gpu` **instead of** `countvision`:

```bash
docker compose --profile nvidia up -d --build countvision-gpu
docker compose logs countvision-gpu | grep "Detector:"     # must say device: cuda
```

If it says `device: cpu`, ONNX Runtime could not load CUDA: the CUDA version of the base image
and `onnxruntime-gpu` do not match (see the comment at the top of `Dockerfile.nvidia`).

## Privacy and licences in the image

- The CPU image contains **no AGPL code** (no Ultralytics). Model: YOLOX-Tiny, Apache-2.0.
- ONNX Runtime's telemetry and Ultralytics' usage events are switched off by the package.
- The container stores no pictures. `status.json` and the logs contain no passwords
  (camera addresses are shown as `rtsp://***@host/...`).

## What was tested (phase 2 B, 2026-10-05, Linux sandbox without GPU)

| Test | Result |
|---|---|
| Build CPU image (Ubuntu 24.04 base, ONNX Runtime, YOLOX-Tiny inside) | OK, 655 MB compressed |
| `demo` self-test inside the container | PASS |
| Two RTSP test cameras + counting service (`--profile test`) | both cameras 10 FPS, `healthy` |
| Stop test camera 37 s, start again | `offline` at once, both `running` again 12–16 s after the camera was back |
| Web app in the container (`setup`), key link, preview | OK, people pixelated, 640 px |
| Data folders owned by root (Linux pitfall) | clear error message with the `chown` fix |
| `install.sh` from a local checkout, test cameras, re-run as update | OK, settings kept, `countvision status` healthy |
| NVIDIA image | install steps OK on a plain Ubuntu base (CPU fallback); **not run on a GPU** |
| Jetson image | **not tested** |
| `install.ps1` | parsed without errors (PowerShell 7 on Linux); **not run on Windows yet** |

GitHub Actions repeats the image build, the test cameras and the outage test on every push (job `docker`).
