# CountVision

CountVision turns a shop's existing cameras into visitor and traffic numbers.

- A small program on site (the **edge agent**) watches the camera, detects and tracks people and vehicles, and counts them.
- **Only numbers leave the building.** No video, no pictures, no faces are stored or sent.
- Counts are saved in a local SQLite file, in 1-minute steps.

## Status

Phase 1, Session A: the edge agent (`edge/`). Web dashboard and cloud come in later sessions.

## Folders

| Folder | What it is |
|---|---|
| `edge/` | The edge agent (Python). See `edge/README.md`. |
| `.github/workflows/` | CI: lint and tests on every push (CPU only). |

## Licence

AGPL-3.0-or-later (see `LICENSE`). The Ultralytics YOLO models are AGPL too. For a closed-source product you need a permissive model (RF-DETR, Apache-2.0) or an Ultralytics Enterprise licence. The detector is swappable on purpose.
