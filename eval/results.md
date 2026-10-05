# CountVision evaluation results (Phase 2, Session A)

Date: 2026-10-03. Code: countvision-edge 0.3.0. Everything below was measured, nothing is estimated.
The tables between the AUTO markers are written by `countvision-edge eval report`.

## Summary in 8 lines

1. **Targets are not met yet.** Line counting on the 4 test clips: **52.9 %** count accuracy with
   YOLOX-Tiny and YOLO11n, **64.7 %** with YOLOX-S. Target: ≥ 95 % (clear scenes).
2. Where the camera view is good (one person at a time, clear path across the line), counting is
   **exact: 6 / 6** with every model.
3. The misses have two clear causes (details below): **(a)** COCO models do not recognise cars seen
   from straight above, **(b)** a line placed where people only *appear* (they come out from behind
   a wall already on the line) cannot be counted. Both are fixed by camera/line placement or a
   custom model, not by tracker settings.
4. **Occupancy** error on the test clips: **4.2 %** (YOLOX-Tiny, target ≤ 5 %). On the crowded shop
   aisle alone: 5.5 % (people hidden behind shelves and each other).
5. **Two real bugs were found and fixed** (before → after): the frame scheduler analysed a 12 FPS
   camera at only 6 FPS, and a person who crossed the line while the tracker was still confirming
   them was not counted.
6. **Tuning the tracker did not help measurably**: with only 7 labeled crossings in the tune clips,
   the default settings were as good as any of the 432 tried combinations. Defaults stay.
7. **Speed:** YOLOX-Tiny runs at **24 FPS** on a weak 2-core cloud CPU (target ≥ 10 FPS for one
   camera). Your laptop and the T4 GPU numbers are still to be measured (`eval\laptop.bat`, Colab).
8. **Recommendation: YOLOX-Tiny (Apache-2.0) as the default detector**, YOLOX-S where the hardware
   allows it. Same accuracy as YOLO11n here, faster on CPU, and no AGPL problem.

## What was evaluated

- **6 clips, 6 minutes of video, 24 true line crossings, 55 occupancy samples.** All are Intel
  IoT DevKit sample videos, licence **CC BY 4.0** (commercial use allowed with credit; credits are
  in each label file). Pexels could not be downloaded from the build machine; Pexels clips (night,
  rain, crowd) are the next set (see `eval/README.md`, "Set B").
- **Split:** 2 tune clips (7 crossings) and 4 test clips (17 crossings, 42 occupancy samples). The
  tuning command refuses test clips. Test clips were run once per model with the final settings.
- **Labels** were made by Claude from frame sheets (1–8 frames per second, zoomed on the line).
  Not yet checked by a second person (`Verified: no`). One clip's crossing *times* were corrected
  after a second, more careful look at the frames; the number of crossings did not change.
- **Live conditions:** frames are picked by the same scheduler as the live app (10 FPS), the same
  tracker and the same line/zone code run. Only the detector output is replayed from a cache.
- **Metrics:** *count accuracy* = 1 − |counted − true| / true, per line, direction and class (what a
  customer sees). *Event F1* also checks that each count happened within 2 s of a real crossing, so
  a miss and a false count cannot cancel out. *Occupancy error* = error of the average number of
  people in a zone over all samples.

**How sure are these numbers?** Not very. 17 test crossings is a small sample: the true accuracy of
"9 of 17" is somewhere between about 30 % and 75 % (95 % interval). The numbers show *where* the
system fails and *why*; they are not yet a number to put on a sales page.

## Why counts were missed (test clips)

| Clip | Missed | Cause (checked frame by frame) | Fix |
|---|---|---|---|
| `car-detection` | 4 / 4 (YOLOX-S: 2 / 4) | Cars seen from straight above are detected as **"cell phone"** (score 0.7–0.9) by the COCO models. | Do not sell top-down vehicle counting with COCO models. Mount the camera at an angle (30–60°), or train a custom vehicle model (Phase 5 A). |
| `face-demographics-walking` | 4 / 7 | People step out from behind a wall **right on the line**; the tracker first sees them on the far side. The group of 3 crosses side by side within 0.5 s. | Installation rule: the line needs at least ~1.5 m of visible walkway on **both** sides. The line was **not** moved after seeing the result (that would be tuning on the test set). |
| `one-by-one-person-detection` | 0 / 6 | — | — |

Tune clips (allowed to look at): the fast walker at 43.9 s in `people-detection` was missed before
the fix (the tracker first reported him right on the line); the white car at 16.2 s in the parking lot is not
detected at all by YOLOX-Tiny and YOLO11n (top view again); YOLOX-S finds it.

## What changed in the code (before → after)

1. **Frame scheduler keeps its rhythm** (`scheduler.py`). Before, every frame interval was rounded up
   to whole source frames: a 12 FPS camera with target 10 FPS was analysed at **6 FPS**, a 25 FPS
   camera at 8.3 FPS. Now the effective rate equals the target (tested for 8–60 FPS sources).
2. **Line counting starts at the first detection** (`tracking.py`, `analytics/lines.py`). A track is
   reported only after 3 frames, and ByteTrack gives a new object its id one frame late. If the
   person crossed the line in that time, the crossing was lost. Now the very first box is
   remembered and used as the start position. This recovered the fast walker (tune clip); together
   with fix 1 it gave one more correct count on the corridor test clip (2 → 3 of 7).
3. **New permissive detector: YOLOX** (Apache-2.0, official ONNX files, `decoder: yolox`), checked
   against YOLO11n on real frames (same objects found, box IoU mostly 0.8–0.98).

<!-- AUTO:START (countvision-edge eval report rewrites this part) -->

### Clips

| Clip | Split | Scene | Length | Classes | True crossings | Occupancy samples | Tags | Verified |
|---|---|---|---|---|---|---|---|---|
| `people-detection` | tune | clear | 50 s | person | 3 (1 in / 2 out) | 13 | indoor, elevated, few-people, near-line-walker | no |
| `person-bicycle-car-detection` | tune | hard | 54 s | person, bicycle, car | 4 (4 in / 0 out) | 0 | outdoor, high-overhead, small-objects, multi-class, cyclists-as-distractors | no |
| `car-detection` | test | clear | 30 s | car | 4 (2 in / 2 out) | 0 | outdoor, overhead, vehicles, wet-road, oncoming-traffic | no |
| `face-demographics-walking` | test | clear | 61 s | person | 7 (7 in / 0 out) | 0 | indoor, corridor, low-camera, walking-towards-camera, groups | no |
| `one-by-one-person-detection` | test | clear | 139 s | person | 6 (0 in / 6 out) | 26 | indoor, counter, dwell, close-to-camera | no |
| `store-aisle-detection` | test | hard | 65 s | person | 0 (0 in / 0 out) | 16 | indoor, shop, occlusion, crowd-small, occupancy-only | no |

### Tuning (tune clips only)

- `onnx:yolox_nano.onnx` on people-detection, person-bicycle-car-detection: 432 combinations in 82.2 s. Default params: 71.4 % count accuracy, best: 71.4 % (F1 83.3 %). Changed: nothing (defaults were best)
- `onnx:yolox_s.onnx` on people-detection, person-bicycle-car-detection: 432 combinations in 86.2 s. Default params: 100.0 % count accuracy, best: 100.0 % (F1 100.0 %). Changed: nothing (defaults were best)
- `onnx:yolox_tiny.onnx` on people-detection, person-bicycle-car-detection: 432 combinations in 86.1 s. Default params: 85.7 % count accuracy, best: 85.7 % (F1 92.3 %). Changed: nothing (defaults were best)
- `ultralytics:yolo11n.pt` on people-detection, person-bicycle-car-detection: 432 combinations in 80.9 s. Default params: 85.7 % count accuracy, best: 85.7 % (F1 92.3 %). Changed: `track_activation_threshold=0.6`, `min_track_frames=5`

### Before → after

_before_ = Phase 1 code with default params. _after_ = current code with the params tuned on the tune clips. Read the test rows: the test clips were never used for tuning.

| Detector | Split | Count accuracy | Event F1 | Occupancy error (avg) | Occupancy MAE |
|---|---|---|---|---|---|
| `onnx:yolox_nano.onnx` | test | 35.3 % → **41.2 %** | 52.2 % → 58.3 % | 11.1 % → 11.1 % | 0.19 → 0.19 |
| `onnx:yolox_nano.onnx` | tune | 57.1 % → **71.4 %** | 72.7 % → 83.3 % | 0.0 % → 0.0 % | 0.00 → 0.00 |
| `onnx:yolox_tiny.onnx` | test | 47.1 % → **52.9 %** | 64.0 % → 69.2 % | 4.2 % → 4.2 % | 0.07 → 0.07 |
| `onnx:yolox_tiny.onnx` | tune | 71.4 % → **85.7 %** | 83.3 % → 92.3 % | 7.7 % → 7.7 % | 0.08 → 0.08 |
| `ultralytics:yolo11n.pt` | test | 47.1 % → **52.9 %** | 64.0 % → 69.2 % | 4.2 % → 4.2 % | 0.12 → 0.12 |
| `ultralytics:yolo11n.pt` | tune | 71.4 % → **85.7 %** | 83.3 % → 92.3 % | 15.4 % → 7.7 % | 0.15 → 0.23 |

### All accuracy runs

| Run | Detector | Params | Split | Count acc. | Clear | Hard | Event F1 | Counted / true | Occ. error (avg) | Occ. MAE |
|---|---|---|---|---|---|---|---|---|---|---|
| before_onnx_yolox_nano.onnx_test | `onnx:yolox_nano.onnx` | before | test | 35.3 % | 35.3 % | – | 52.2 % | 6 / 17 | 11.1 % | 0.19 |
| after_onnx_yolox_nano.onnx_test | `onnx:yolox_nano.onnx` | after | test | 41.2 % | 41.2 % | – | 58.3 % | 7 / 17 | 11.1 % | 0.19 |
| after_onnx_yolox_s.onnx_test | `onnx:yolox_s.onnx` | after | test | 64.7 % | 64.7 % | – | 78.6 % | 11 / 17 | 6.9 % | 0.17 |
| before_onnx_yolox_tiny.onnx_test | `onnx:yolox_tiny.onnx` | before | test | 47.1 % | 47.1 % | – | 64.0 % | 8 / 17 | 4.2 % | 0.07 |
| after_onnx_yolox_tiny.onnx_test | `onnx:yolox_tiny.onnx` | after | test | 52.9 % | 52.9 % | – | 69.2 % | 9 / 17 | 4.2 % | 0.07 |
| laptop_yolox_tiny_test | `onnx:yolox_tiny.onnx` | – | test | 52.9 % | 52.9 % | – | 69.2 % | 9 / 17 | 4.2 % | 0.07 |
| before_ultralytics_yolo11n.pt_test | `ultralytics:yolo11n.pt` | before | test | 47.1 % | 47.1 % | – | 64.0 % | 8 / 17 | 4.2 % | 0.12 |
| after_ultralytics_yolo11n.pt_test | `ultralytics:yolo11n.pt` | after | test | 52.9 % | 52.9 % | – | 69.2 % | 9 / 17 | 4.2 % | 0.12 |
| size_ultralytics_yolo11n.pt_416_test | `ultralytics:yolo11n.pt@416` | – | test | 47.1 % | 47.1 % | – | 64.0 % | 8 / 17 | 8.3 % | 0.14 |
| before_onnx_yolox_nano.onnx_tune | `onnx:yolox_nano.onnx` | before | tune | 57.1 % | 66.7 % | 50.0 % | 72.7 % | 4 / 7 | 0.0 % | 0.00 |
| after_onnx_yolox_nano.onnx_tune | `onnx:yolox_nano.onnx` | after | tune | 71.4 % | 100.0 % | 50.0 % | 83.3 % | 5 / 7 | 0.0 % | 0.00 |
| after_onnx_yolox_s.onnx_tune | `onnx:yolox_s.onnx` | after | tune | 100.0 % | 100.0 % | 100.0 % | 100.0 % | 7 / 7 | 7.7 % | 0.08 |
| before_onnx_yolox_tiny.onnx_tune | `onnx:yolox_tiny.onnx` | before | tune | 71.4 % | 66.7 % | 75.0 % | 83.3 % | 5 / 7 | 7.7 % | 0.08 |
| after_onnx_yolox_tiny.onnx_tune | `onnx:yolox_tiny.onnx` | after | tune | 85.7 % | 100.0 % | 75.0 % | 92.3 % | 6 / 7 | 7.7 % | 0.08 |
| before_ultralytics_yolo11n.pt_tune | `ultralytics:yolo11n.pt` | before | tune | 71.4 % | 66.7 % | 75.0 % | 83.3 % | 5 / 7 | 15.4 % | 0.15 |
| after_ultralytics_yolo11n.pt_tune | `ultralytics:yolo11n.pt` | after | tune | 85.7 % | 100.0 % | 75.0 % | 92.3 % | 6 / 7 | 7.7 % | 0.23 |
| size_ultralytics_yolo11n.pt_416_tune | `ultralytics:yolo11n.pt@416` | – | tune | 100.0 % | 100.0 % | 100.0 % | 100.0 % | 7 / 7 | 7.7 % | 0.08 |

#### Per clip: after_onnx_yolox_nano.onnx_test (`onnx:yolox_nano.onnx`)

| Clip | Scene | Counted / true | Count acc. | Event F1 | Occ. counted / true | Occ. error (avg) | Occ. MAE |
|---|---|---|---|---|---|---|---|
| `car-detection` | clear | 0 / 4 | 0.0 % | 0.0 % | – | – | – |
| `face-demographics-walking` | clear | 1 / 7 | 14.3 % | 25.0 % | – | – | – |
| `one-by-one-person-detection` | clear | 6 / 6 | 100.0 % | 100.0 % | 17 / 17 | 0.0 % | 0.00 |
| `store-aisle-detection` | hard | 0 / 0 | – | – | 47 / 55 | 14.5 % | 0.50 |

#### Per clip: after_onnx_yolox_s.onnx_test (`onnx:yolox_s.onnx`)

| Clip | Scene | Counted / true | Count acc. | Event F1 | Occ. counted / true | Occ. error (avg) | Occ. MAE |
|---|---|---|---|---|---|---|---|
| `car-detection` | clear | 2 / 4 | 50.0 % | 66.7 % | – | – | – |
| `face-demographics-walking` | clear | 3 / 7 | 42.9 % | 60.0 % | – | – | – |
| `one-by-one-person-detection` | clear | 6 / 6 | 100.0 % | 100.0 % | 17 / 17 | 0.0 % | 0.00 |
| `store-aisle-detection` | hard | 0 / 0 | – | – | 50 / 55 | 9.1 % | 0.44 |

#### Per clip: after_onnx_yolox_tiny.onnx_test (`onnx:yolox_tiny.onnx`)

| Clip | Scene | Counted / true | Count acc. | Event F1 | Occ. counted / true | Occ. error (avg) | Occ. MAE |
|---|---|---|---|---|---|---|---|
| `car-detection` | clear | 0 / 4 | 0.0 % | 0.0 % | – | – | – |
| `face-demographics-walking` | clear | 3 / 7 | 42.9 % | 60.0 % | – | – | – |
| `one-by-one-person-detection` | clear | 6 / 6 | 100.0 % | 100.0 % | 17 / 17 | 0.0 % | 0.00 |
| `store-aisle-detection` | hard | 0 / 0 | – | – | 52 / 55 | 5.5 % | 0.19 |

#### Per clip: after_ultralytics_yolo11n.pt_test (`ultralytics:yolo11n.pt`)

| Clip | Scene | Counted / true | Count acc. | Event F1 | Occ. counted / true | Occ. error (avg) | Occ. MAE |
|---|---|---|---|---|---|---|---|
| `car-detection` | clear | 0 / 4 | 0.0 % | 0.0 % | – | – | – |
| `face-demographics-walking` | clear | 3 / 7 | 42.9 % | 60.0 % | – | – | – |
| `one-by-one-person-detection` | clear | 6 / 6 | 100.0 % | 100.0 % | 17 / 17 | 0.0 % | 0.00 |
| `store-aisle-detection` | hard | 0 / 0 | – | – | 52 / 55 | 5.5 % | 0.31 |

### Speed

**laptop** — Intel(R) Xeon(R) CPU E3-1505M v6 @ 3.00GHz, 4 cores / 8 threads, 15.8 GB RAM, GPU: Quadro M2200, Windows 11. 60 frames of `store-aisle-detection.mp4` resized to 1280x720. Date: 2026-10-05.

| Model | Licence | Runtime | Input | Detector ms (mean / p95) | Detector FPS | Pipeline FPS | Cameras @10 / @15 FPS | Note |
|---|---|---|---|---|---|---|---|---|
| `onnx:yolox_nano.onnx` | Apache-2.0 | onnxruntime (cpu) | 416 | 14.5 / 15.9 | 69.0 | 68.4 | 5 / 3 |  |
| `openvino:yolox_nano.onnx` | Apache-2.0 | openvino (cpu) | 416 | 12.0 / 13.9 | 83.0 | 82.3 | 6 / 4 |  |
| `onnx:yolox_tiny.onnx` | Apache-2.0 | onnxruntime (cpu) | 416 | 34.9 / 41.7 | 28.6 | 28.5 | 2 / 1 |  |
| `openvino:yolox_tiny.onnx` | Apache-2.0 | openvino (cpu) | 416 | 38.1 / 48.3 | 26.2 | 26.1 | 2 / 1 |  |
| `openvino:yolox_s.onnx` | Apache-2.0 | openvino (cpu) | 640 | 177.3 / 355.1 | 5.6 | 5.6 | 0 / 0 |  |
| `ultralytics:yolo11n.pt@640` | AGPL-3.0 | pytorch (auto) | 640 | 66.2 / 74.1 | 15.1 | 15.0 | 1 / 0 |  |
| `ultralytics:yolo11n.pt@416` | AGPL-3.0 | pytorch (auto) | 416 | 43.9 / 48.1 | 22.8 | 22.7 | 1 / 1 |  |
| `onnx:yolo11n.onnx` | AGPL-3.0 | onnxruntime (cpu) | 640 | 69.4 / 75.0 | 14.4 | 14.4 | 1 / 0 |  |
| `openvino:yolo11n_openvino_model/yolo11n.xml` | AGPL-3.0 | openvino (cpu) | 640 | 70.0 / 76.9 | 14.3 | 14.3 | 1 / 0 |  |

**sandbox-xeon-2core** — Intel(R) Xeon(R) Processor @ 2.80GHz, 2 cores / 2 threads, 7.8 GB RAM, GPU: none, Linux 6.18.44-fc-v64. 40 frames of `store-aisle-detection.mp4` resized to 1280x720. Date: 2026-10-03.

| Model | Licence | Runtime | Input | Detector ms (mean / p95) | Detector FPS | Pipeline FPS | Cameras @10 / @15 FPS | Note |
|---|---|---|---|---|---|---|---|---|
| `onnx:yolox_nano.onnx` | Apache-2.0 | onnxruntime (cpu) | 416 | 13.8 / 15.5 | 72.4 | 71.7 | 6 / 4 |  |
| `openvino:yolox_nano.onnx` | Apache-2.0 | openvino (cpu) | 416 | 16.8 / 23.5 | 59.4 | 58.9 | 5 / 3 |  |
| `onnx:yolox_tiny.onnx` | Apache-2.0 | onnxruntime (cpu) | 416 | 41.0 / 47.3 | 24.4 | 24.3 | 2 / 1 |  |
| `openvino:yolox_tiny.onnx` | Apache-2.0 | openvino (cpu) | 416 | 38.4 / 46.3 | 26.1 | 25.6 | 2 / 1 |  |
| `onnx:yolox_s.onnx` | Apache-2.0 | onnxruntime (cpu) | 640 | 137.9 / 157.2 | 7.3 | 7.2 | 0 / 0 |  |
| `openvino:yolox_s.onnx` | Apache-2.0 | openvino (cpu) | 640 | 135.4 / 164.8 | 7.4 | 7.4 | 0 / 0 |  |
| `ultralytics:yolo11n.pt@640` | AGPL-3.0 | pytorch (auto) | 640 | 72.8 / 80.4 | 13.7 | 13.7 | 1 / 0 |  |
| `ultralytics:yolo11n.pt@416` | AGPL-3.0 | pytorch (auto) | 416 | 33.1 / 41.6 | 30.2 | 30.1 | 2 / 1 |  |
| `ultralytics:yolo11n.pt@320` | AGPL-3.0 | pytorch (auto) | 320 | 25.7 / 30.7 | 38.9 | 38.7 | 3 / 2 |  |
| `onnx:yolo11n_640.onnx` | AGPL-3.0 | onnxruntime (cpu) | 640 | 60.2 / 72.8 | 16.6 | 16.6 | 1 / 0 |  |
| `openvino:yolo11n_640_openvino_model/yolo11n.xml` | AGPL-3.0 | openvino (cpu) | 640 | 53.1 / 64.2 | 18.8 | 18.8 | 1 / 1 |  |
| `onnx:yolo11n_416.onnx` | AGPL-3.0 | onnxruntime (cpu) | 416 | 27.8 / 33.1 | 36.0 | 35.8 | 3 / 2 |  |
| `openvino:yolo11n_416_openvino_model/yolo11n.xml` | AGPL-3.0 | openvino (cpu) | 416 | 23.1 / 27.8 | 43.4 | 43.1 | 3 / 2 |  |

<!-- AUTO:END -->

## Recommendation (with licence impact)

| Use | Model | Licence | Why |
|---|---|---|---|
| **Default (CPU, mini-PC, laptop)** | **YOLOX-Tiny**, ONNX Runtime | **Apache-2.0** code and weights | Same counting accuracy as YOLO11n on these clips, 24 FPS on 2 weak cores, closed-source product allowed (keep the licence text and credit Megvii). |
| Small/far objects, overhead views, stronger PC or GPU | **YOLOX-S** | Apache-2.0 | Best accuracy here (64.7 %, found 2 of 4 top-view cars, 4 / 4 on the parking tune clip). 7 FPS on 2 weak cores: needs a better CPU or a GPU. |
| GPU sites | **RF-DETR Nano / Small** | Apache-2.0 (Nano–Large; XL/2XL are *not* Apache) | Strong modern models; speed and accuracy still to be measured on the Colab T4. |
| Only for AGPL/open-source setups | YOLO11n | **AGPL-3.0** | Not better than YOLOX-Tiny here. In a closed-source paid product it needs an Ultralytics Enterprise licence. |
| Not recommended | YOLOX-Nano | Apache-2.0 | Fastest (72 FPS) but clearly worse (41.2 %, 14.5 % occupancy error in the shop). |

Notes:
- The COCO-trained weights of all these models were trained on the COCO dataset. That is normal
  industry practice; if a big customer asks, get a short legal opinion.
- The **repository itself is AGPL-3.0** today. As the only author you can change the licence of your
  own code later; decide that before Phase 3 if the cloud product will be closed source.
- **Input size:** YOLO11n at 416 instead of 640 is 2.2× faster but lost 1 test crossing and had
  higher occupancy error (8.3 % vs 4.2 % on the test clips). Keep 640 for YOLO11n; YOLOX-Tiny is 416 by design.
- **OpenVINO vs ONNX Runtime** on this CPU: OpenVINO was 12–17 % faster for YOLO11n, about equal for YOLOX.
  Measure on your laptop (Intel CPUs often gain more).

## Still to do (honest list)

- [ ] **Laptop numbers:** run `eval\laptop.bat` (≈ 15 min). Fills the "laptop" speed table.
- [ ] **T4 GPU + RF-DETR:** run `eval/colab/countvision_gpu_benchmark.ipynb` on Colab, unzip results, `countvision-edge eval report`.
- [ ] **Verify labels:** open 2 clips in `count-helper`, check every mark, write your name in `verified_by`.
- [ ] **Set B (Pexels or own footage):** night, rain, crowd, a real shop door. At least 50 crossings
      per scene type before quoting any accuracy number to a customer.
- [ ] **1-hour occupancy test:** the target is "error over 1 hour"; the longest clip here is 2.3 min.
