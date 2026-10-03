# CountVision edge agent

Counts people and vehicles from a camera. Runs on a normal PC or mini-PC. No cloud needed.

## Install

Python 3.10 or newer.

```powershell
cd countvision
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e "edge[dev]"          # core + tests
pip install -e "edge[yolo]"         # optional: Ultralytics YOLO (AGPL)
pip install -e "edge[openvino]"     # optional: fast on Intel CPUs
pip install -e "edge[onnx]"         # optional: ONNX Runtime
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
| `countvision-edge run --config configs/example.yaml --show` | Count live without the web app; `--show` opens a preview window |
| `countvision-edge report --db data/countvision.db` | Print the counts (`--json`, `--csv-dir`) |
| `countvision-edge export --model yolo11n.pt --format openvino` | Convert a YOLO model for faster CPU use (result stays AGPL) |

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
| `blobs` | nothing | demo only |

Models exported from YOLO weights stay AGPL. The licence is logged at start.

## Privacy

Only these are stored: events, counts, zone numbers, coverage, heartbeats, and one heatmap PNG (numbers drawn as colour, no people). No video, no frames.

The web app's live picture is made in memory only while a browser is watching, and is never saved. An uploaded video is deleted when counting ends.

## Known limits

- Speed estimate uses one scale for the whole picture. It is approximate.
- One camera per process (several cameras: Phase 2 Session B). The web app shows one camera.
- The web app has no login. Keep it on 127.0.0.1, or use the key link with `--host 0.0.0.0`.
- Real RTSP and real webcam were tested with fakes only in CI. Please test your own camera.
- The RF-DETR wrapper is untested without weights.

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
