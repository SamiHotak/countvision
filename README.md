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
```

The first start creates `.venv` and installs everything. Then the browser opens
`http://127.0.0.1:8000`: live picture, draw lines and zones with the mouse, big live counters,
today's chart and CSV download.

## Status

- Phase 1, Session A: the edge agent (`edge/`): inputs, detectors, tracking, lines, zones, SQLite.
- Phase 1, Session B: the local web app (`countvision-edge app`).
- Phase 2, Session A: evaluation on hand-labeled clips, speed benchmark, YOLOX (Apache-2.0)
  detector. Results and recommendation: [`eval/results.md`](eval/results.md).
- Cloud dashboard: later phases.

## Folders

| Folder | What it is |
|---|---|
| `edge/` | The edge agent (Python). See `edge/README.md`. |
| `edge/countvision_edge/app/` | The local web app (FastAPI + one plain HTML/JS page). |
| `start.bat` | Windows: install on first start, then open the local app. |
| `eval/` | Hand labels, accuracy and speed results, `laptop.bat`, Colab notebook. See `eval/README.md`. |
| `.github/workflows/` | CI: lint, tests and browser tests on every push (CPU only). |

## Licence

AGPL-3.0-or-later (see `LICENSE`). The Ultralytics YOLO models are AGPL too. For a closed-source product you need a permissive model (YOLOX or RF-DETR, Apache-2.0) or an Ultralytics Enterprise licence. The detector is swappable on purpose.

Evaluation videos: Intel IoT DevKit sample videos (CC BY 4.0). YOLOX models: Megvii (Apache-2.0).
