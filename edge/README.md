# CountVision edge agent

Counts people and vehicles from a camera. Runs on a normal PC or mini-PC. No cloud needed.

## Install

Python 3.10 or newer.

```powershell
cd countvision
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e "edge[onnx]"         # core + ONNX Runtime (default detector YOLOX-Tiny, Apache-2.0)
pip install -e "edge[dev]"          # + tests
pip install -e "edge[openvino]"     # optional: OpenVINO for Intel CPUs
pip install -e "edge[yolo]"         # optional: Ultralytics YOLO (AGPL-3.0)
```

## Try it in one minute (no camera, no model)

```powershell
countvision-edge demo
```

It makes a synthetic video, counts it, and checks the numbers. You should see `PASS`.

## Local web app

```powershell
countvision-edge app --config configs/example.yaml   # or: countvision-edge app --demo
```

Opens `http://127.0.0.1:8000` in your browser:

- **Live picture** with boxes and track numbers. Lines and zones are drawn on top.
- **Edit lines and zones**: drag across the door to draw a line (the arrow shows IN), click corners
  for a zone, drag corners to move them. Undo (Ctrl+Z), redo (Ctrl+Y), snap (S), flip direction (F),
  delete (Del). **Save and count** uses the new lines at once and writes them into the YAML file
  (comments stay; a copy of the old file is kept as `.bak`).
- **Big counters**: In and Out per line for today, people inside and waiting per zone, visits and
  average stay.
- **Chart**: In and Out per hour today (per minute for a video).
- **Source**: switch to another webcam, or upload a video file to count it. The video is counted
  on this PC and deleted afterwards. Video numbers are stored under `<camera id>-video`, so they
  never mix with the real camera.
- **Download**: hourly CSV (opens in Excel), all raw tables as ZIP, or a text report.

Options: `--port 8001`, `--source 1` (another webcam or a file), `--preview blur` (people blurred in
the picture) or `--preview off`, `--host 0.0.0.0` (open it on a phone in the same Wi-Fi; the console
prints a link with a random key, nobody can open the page without it). By default the app only
listens on this PC (127.0.0.1).

## Commands

| Command | What it does |
|---|---|
| `countvision-edge demo` | Self-test on a synthetic video |
| `countvision-edge probe` | Find which webcam numbers work (0, 1, 2 ...) |
| `countvision-edge snapshot --source 0 --out snap.jpg` | Save one frame (stays on this PC) to help you place lines |
| `countvision-edge app --config configs/example.yaml` | Local web app: preview, draw lines, live counters, chart, CSV |
| `countvision-edge app --demo` | The web app with a synthetic demo video |
| `countvision-edge run --config site.yaml` | Count **all cameras** of the config (restarts, reconnects, `data/status.json`, offline alerts). `--camera <id>` for one |
| `countvision-edge run --config configs/example.yaml --show` | One camera with a preview window (debugging) |
| `countvision-edge health --config site.yaml` | Is counting running? Exit code 0 = healthy, 1 = not running, 2 = a camera alert |
| `countvision-edge pair --config site.yaml --url https://<cloud> --code XXXX-XXXX` | Connect this device to the CountVision cloud (code from the web app: Devices → Add device) |
| `countvision-edge cloud-status --config site.yaml` | Is the cloud reachable, how many rows wait for upload |
| `countvision-edge upload --config site.yaml` | Send the waiting numbers now (normally `run` does it every 15 s) |
| `countvision-edge unpair --config site.yaml` | Forget the cloud connection (count locally only) |
| `countvision-edge models list` / `models download yolox_tiny` | Known models (Apache-2.0); a missing known model is downloaded on first start |
| `countvision-edge pilot-docs --info pilot.yaml` | Pilot papers: sign, privacy notice, agreement, AVV, DPIA, checklist (`docs/pilot/`) |
| `countvision-edge report --db data/countvision.db` | Print the counts (`--json`, `--csv-dir`) |
| `countvision-edge export --model yolo11n.pt --format openvino` | Convert a YOLO model for faster CPU use (result stays AGPL) |
| `countvision-edge count-helper VIDEO` | Label the true counts of a video by hand (window, keys I/O/0-9) |
| `countvision-edge eval fetch / tune / run / report` | Accuracy on hand-labeled clips, tuning on tune clips only, `eval/results.md` |
| `countvision-edge bench --model onnx:yolox_tiny.onnx` | Speed of detector x runtime x input size on this PC |

Evaluation details and results: `eval/README.md` and `eval/results.md`.

Use `--help` on any command.

## Config

Copy `configs/example.yaml` and edit it. Lines and zones use numbers from 0 to 1 (0,0 = top-left).
`in_direction: to_right` means: walking from the left side to the right side of the line (looking from p1 to p2) counts as IN.
Secrets such as camera passwords go in environment variables: `${CAM1_RTSP_URL}`.

## Detectors and licences

| `detector.type` | Needs | Licence |
|---|---|---|
| `ultralytics` | `edge[yolo]` | AGPL-3.0 (code and weights) |
| `rfdetr` | `edge[rfdetr]` | Apache-2.0 for standard checkpoints |
| `onnx` / `openvino` | a model file | the licence of that model (set `model_license`) |
| `onnx` + YOLOX file | `edge[onnx]`, e.g. `yolox_tiny.onnx` | **Apache-2.0** (code and weights). Recommended default, see `eval/results.md` |
| `blobs` | nothing | demo only |

Models exported from YOLO weights stay AGPL. The licence is logged at start.

## Several cameras (supervisor)

`countvision-edge run` without `--show` starts one worker per camera. All share one detector
(one model in memory) and one database. A crashed camera is restarted (1 s ... 60 s delay), a lost
stream reconnects by itself (≤ 15 s backoff). Every 5 s `data/status.json` is written.
A camera offline longer than `alerts.camera_offline_after_s` (default 300 s) gives a WARNING,
`alert` in status.json and a `camera_offline` event (`camera_online` with the outage when back).

Measured (phase 2 B, 2-core sandbox, YOLOX-Tiny, two real RTSP streams from MediaMTX): both
cameras at 10 FPS; stream stopped → `offline` within 1 s; stream back → counting again after
12–16 s; the other camera kept counting during the outage.

## Cloud upload

After `countvision-edge pair ...` the file `data/cloud.json` holds the cloud address and this
device's token (keep it private). `countvision-edge run` then uploads every `cloud.upload_interval_s`
(15 s): minute counts, zone numbers, coverage, events, the newest heartbeat and the camera states.
**Only numbers** — never images or video.

- No network? Counting goes on; rows stay in `data/countvision.db` (`sent = 0`) and are sent when
  the cloud is reachable again (several uploads in a row, retry 2 s … 60 s). `health` shows
  `cloud: offline, N rows waiting`.
- Sending something twice is safe: the cloud overwrites minute rows and ignores known events.
- Removed in the web app? The upload stops (`cloud: revoked`), counting continues locally.
- New line crossings go up within about 1 s while the cloud is reachable (live counters).
- `cloud.url` / `CV_CLOUD_URL` overrides the address, e.g. `http://host.docker.internal:3000`
  when the agent runs in Docker and the cloud on the same PC.
- The local data is still deleted after `storage.retention_days`, sent or not. A device that is
  offline longer than that loses the oldest numbers.

Tested (phase 3 B, sandbox): two cameras uploaded the demo video, the cloud showed exactly
IN 4 / OUT 2 per camera; cloud stopped, counted again (13 rows waiting), cloud started,
`upload`: cloud IN 8 / OUT 4 = edge report, nothing lost, nothing doubled.

## Lines, zones and counting hours from the cloud (phase 3 C)

When the device is paired, `run` also keeps one open request to the cloud (a long-poll, 25 s).
When somebody saves lines/zones/classes/counting hours in the cloud editor, the device gets them
**within about a second**, applies them to the **running** camera (from the next frame; totals of
lines with the same name are kept) and reports the version back with an immediate upload.

- The cloud version is saved in `data/cloud_config.json` and used at every start, on top of the
  YAML config (source, detector and tuning stay from the YAML). Delete that file to go back to the
  YAML lines. `cloud.config_sync: false` ignores the cloud editor completely.
- A config the model cannot use (e.g. a class it does not know) is refused; the old config keeps
  counting and the web app shows the reason.
- Counting hours (`schedule:` per camera: `days` 0 = Monday … 6 = Sunday, `start`, `end`,
  `timezone`; end before start = over midnight): outside, frames are read but not analysed and the
  camera shows `paused`. The cloud sends the site's time zone.
- Snapshots: only when a user clicks **Take snapshot** in the web app. The device runs a fresh
  detection on the newest frame (ALL classes, so people in a car park are hidden too), pixelates
  every box, scales to max 960 px and sends ONE JPEG. Nothing is saved here; the cloud keeps it 10
  minutes in memory. `privacy.snapshots: false` refuses all snapshot requests.
- New line crossings are uploaded within ~1 s (not only every `upload_interval_s`), so the web
  app's counters are live.

Measured (sandbox, edge agent with an MJPEG test stream + real cloud through the web server):
crossing → browser 0.6–0.95 s (median 0.77 s, 12 crossings); line saved in the browser →
applied on the edge and reported back 0.48 s; snapshot 0.6 s; the new line counted at once;
config saved while the agent was stopped → applied right after its next start.

## Privacy

Only these are stored: events, counts, zone numbers, coverage, heartbeats, and one heatmap PNG (numbers drawn as colour, no people). No video, no frames.

The web app's live picture is made in memory only while a browser is watching, and is never saved. An uploaded video is deleted when counting ends.
**Default `privacy.preview: blur`**: every detected person/vehicle is pixelated (also fresh detections
that are not tracks yet) and the picture is limited to 640 px. `off` = no picture, `full` = setup only.

No library phones home: ONNX Runtime telemetry (`ORT_DISABLE_TELEMETRY=1`) and Ultralytics events
(`YOLO_OFFLINE=1`) are switched off when the package is imported. (ONNX Runtime 1.30 on Linux was
seen sending telemetry to Microsoft before this.)

## Known limits

- Speed estimate uses one scale for the whole picture. It is approximate.
- The web app shows one camera at a time (`--camera <id>`). Do not run the web app and
  `run` on the same cameras at the same time: everything would be counted twice.
- The web app has no login. Keep it on 127.0.0.1, or use the key link with `--host 0.0.0.0`.
- After lines were saved in the cloud editor, `run` uses the cloud version (`data/cloud_config.json`).
  The local web app (`app`) still shows and edits the YAML lines. Use one place to draw lines.
- RTSP was tested with MediaMTX test streams (phase 2 B), not yet with a real IP camera.
- The RF-DETR wrapper is untested without weights (measured on Colab with `eval/colab/`).
- COCO models do not recognise cars filmed from straight above (they see "cell phone"). Mount
  vehicle cameras at an angle.
- A line needs about 1.5 m of visible path on both sides. People who appear right on the line
  (from behind a wall or door) are often not counted.

## Tests

```powershell
pytest edge/tests
ruff check edge
```

Browser tests of the web app (optional, Chromium via Playwright):

```powershell
pip install -e "edge[dev,e2e]"
python -m playwright install chromium
pytest edge/tests/e2e
```
