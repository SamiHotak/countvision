@echo off
rem CountVision - evaluation on your Windows laptop, one command.
rem   eval\laptop.bat           speed benchmark on this laptop + accuracy of the default model
rem   eval\laptop.bat --quick   speed benchmark only (about 5 minutes)
rem Results: eval\bench\laptop.json, eval\runs\*.json, tables in eval\results.md
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first. It creates the Python environment.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"

echo [1/5] Installing the CPU runtimes (ONNX Runtime, OpenVINO) ...
pip install -q -e "edge[yolo,onnx,openvino]"
if errorlevel 1 goto failed

echo [2/5] Downloading the 6 test videos (Intel sample videos, CC BY 4.0) ...
countvision-edge eval fetch
if errorlevel 1 goto failed

echo [3/5] Downloading the YOLOX models (Apache-2.0) and exporting YOLO11n (AGPL-3.0) ...
if not exist "eval\models" mkdir "eval\models"
for %%M in (yolox_nano yolox_tiny yolox_s) do (
  if not exist "eval\models\%%M.onnx" curl.exe -L -s -o "eval\models\%%M.onnx" "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/%%M.onnx"
)
if not exist "eval\models\yolo11n_openvino_model\yolo11n.xml" (
  pushd eval\models
  countvision-edge export --model yolo11n.pt --format openvino --imgsz 640
  countvision-edge export --model yolo11n.pt --format onnx --imgsz 640
  popd
)

echo [4/5] Speed benchmark on this laptop (close other programs, plug in the charger) ...
countvision-edge bench --label laptop --video eval\videos\store-aisle-detection.mp4 ^
  --model onnx:yolox_nano.onnx --model openvino:yolox_nano.onnx ^
  --model onnx:yolox_tiny.onnx --model openvino:yolox_tiny.onnx ^
  --model openvino:yolox_s.onnx ^
  --model ultralytics:yolo11n.pt@640 --model ultralytics:yolo11n.pt@416 ^
  --model onnx:yolo11n.onnx --model openvino:yolo11n_openvino_model/yolo11n.xml
if errorlevel 1 goto failed

if /I "%~1"=="--quick" goto report
echo [5/5] Accuracy of YOLOX-Tiny on the test clips (about 5 minutes) ...
countvision-edge eval run --detector onnx:yolox_tiny.onnx --split test --name laptop_yolox_tiny_test --params eval\params\onnx_yolox_tiny.onnx.yaml --note "Ezat's laptop"

:report
countvision-edge eval report
echo.
echo Done. Open eval\results.md. Then commit and push (see the session notes).
pause
goto end

:failed
echo.
echo Something failed. Scroll up to see the error and send it to your engineer.
pause
exit /b 1

:end
endlocal
