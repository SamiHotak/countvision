# Evaluation: how good is the counting, really?

This folder holds the **hand labels**, the **results** and the tools' output. Videos, models and
caches are NOT in git (they are downloaded or created locally).

| Path | What | In git? |
|---|---|---|
| `clips/*.yaml` | One label file per clip: lines, zones, true crossings, true occupancy, licence, credit | yes |
| `results.md` | The report (tables are rewritten by `countvision-edge eval report`) | yes |
| `runs/*.json` | Every accuracy run and tuning run | yes |
| `bench/*.json` | Every speed benchmark (one file per machine) | yes |
| `params/*.yaml` | Best settings per detector, found on the **tune** clips | yes |
| `laptop.bat` | Windows: speed + accuracy on your laptop, one command | yes |
| `colab/countvision_gpu_benchmark.ipynb` | Google Colab: T4 GPU speed + RF-DETR accuracy | yes |
| `videos/`, `models/`, `cache/` | Downloaded videos and models, cached detections | **no** |

## The rules (why the numbers can be trusted)

1. **Never tune on the test set.** Every clip has `split: tune` or `split: test`. `eval tune` refuses
   test clips. Run the test clips once, with the final settings, and report what comes out.
2. **Labels are what a human sees**, never what the model says. `count-helper` does not show any
   detections on purpose.
3. **Report misses honestly**, with the reason (look at the frames).
4. **Licence of every clip** is in its label file. Only use clips you may use (CC BY, CC0, Pexels
   licence, or your own footage with permission of the people in it).

## Commands (copy-paste, PowerShell or cmd, in the repo folder, venv active)

```powershell
countvision-edge eval fetch                                   # download the videos
countvision-edge eval tune --detector onnx:yolox_tiny.onnx    # grid search on the TUNE clips
countvision-edge eval run  --detector onnx:yolox_tiny.onnx --split test --tag after --params eval\params\onnx_yolox_tiny.onnx.yaml
countvision-edge bench --label laptop --model onnx:yolox_tiny.onnx --video eval\videos\store-aisle-detection.mp4
countvision-edge eval report                                  # update eval\results.md
```

Detector format: `type:model[@input size]`, for example `onnx:yolox_tiny.onnx`,
`openvino:yolox_s.onnx`, `ultralytics:yolo11n.pt@416`, `rfdetr:nano`. Model files are looked up in
`eval\models\`.

The first run of a detector on a clip is slow (it runs the model on every needed frame). The
result is cached in `eval\cache\`, so tuning 432 settings then takes about 1–2 minutes.

## Label a new clip with count-helper

1. Put the video into `eval\videos\` (for example `shop-door.mp4`).
2. Start the helper:

   ```powershell
   countvision-edge count-helper eval\videos\shop-door.mp4 --split test --scene clear --labeled-by Ezat
   ```

   For vehicles add `--classes car,truck,bus --anchor center`.
3. In the window:
   - Press **T**, click 2 points = a counting line. The green arrow shows the **IN** side. **F** flips it.
   - Press **T** twice, click the corners, **right-click** = a zone (for occupancy).
   - **SPACE** play/pause, **A/D** or arrows ±1 frame, **J/L** ±1 second, **[ ]** slower/faster.
   - When a person's feet (or the box centre with `--anchor center`) cross the line: pause, step to
     the exact frame, press **I** (in) or **O** (out). **C** changes the class.
   - Occupancy: press **G** (jumps 10 s), then type how many people are in the active zone (**0–9**,
     **+/-** for more). **N** switches between lines and zones.
   - **X** deletes the nearest mark, **U** undoes. **, .** jump between marks. **ESC** quits.
   - Everything is saved after every key press to `eval\clips\shop-door.yaml`.
4. Open the YAML file and fill in `title`, `source_url`, `license`, `credit` (and `download_url` if
   the video can be downloaded directly).
5. Ask a second person to check it, then set `verified_by`.

Tips: label at 0.25× speed in busy scenes. Count a person who turns back before the line as nothing.
A person who crosses, turns around and crosses back is one IN and one OUT.

## Set B: clips you still need (night, rain, crowd, real shop door)

The current 6 clips are daytime, mostly calm. The quality targets also need hard scenes:

| Scene | Where to get it | How many crossings |
|---|---|---|
| Night / dark street | Pexels search "night street walking", "people walking at night" | ≥ 50 |
| Rain, umbrellas | Pexels "rain people walking umbrella" | ≥ 50 |
| Crowd (station, mall) | Pexels "crowd walking", "mall people" | ≥ 100 |
| Real shop door | Your own footage, with permission and a sign at the door | ≥ 50 |

Pexels videos are free for commercial use under the Pexels licence (no credit needed, but write
the link into `source_url`). Download them by hand in the browser, save into `eval\videos\`,
label with count-helper.

Mark 1 of every 3 new clips as `split: tune`, the rest `split: test`, **before** you run anything.

## Credits

Clips in `clips/` without "own footage": Intel IoT DevKit sample videos,
https://github.com/intel-iot-devkit/sample-videos, licence CC BY 4.0.
YOLOX models: Megvii, https://github.com/Megvii-BaseDetection/YOLOX, Apache-2.0.
