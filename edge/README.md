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

## Commands

| Command | What it does |
|---|---|
| `countvision-edge demo` | Self-test on a synthetic video |
| `countvision-edge probe` | Find which webcam numbers work (0, 1, 2 ...) |
| `countvision-edge snapshot --source 0 --out snap.jpg` | Save one frame (stays on this PC) to help you place lines |
| `countvision-edge run --config configs/example.yaml --show` | Count live; `--show` opens a preview window |
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

## Known limits

- Speed estimate uses one scale for the whole picture. It is approximate.
- One camera per process (several cameras: later session).
- Real RTSP and real webcam were tested with fakes only in CI. Please test your own camera.
- The RF-DETR wrapper is untested without weights.

## Tests

```powershell
pytest edge/tests
ruff check edge
```
