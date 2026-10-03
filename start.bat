@echo off
rem CountVision local app - one command on Windows.
rem   start.bat            count with your webcam (config: edge\configs\local.yaml)
rem   start.bat --demo     demo video, no camera and no model needed
rem   start.bat --phone    also open it on your phone (same Wi-Fi, needs the key link)
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto venv_ok
echo Creating the Python environment - first start only ...
python -m venv .venv
if errorlevel 1 goto no_python
:venv_ok
call ".venv\Scripts\activate.bat"

python -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('countvision_edge.app.main', 'fastapi', 'ruamel.yaml', 'ultralytics')) else 1)" 1>nul 2>nul
if not errorlevel 1 goto installed
echo Installing CountVision - first start or after an update. This can take a few minutes ...
python -m pip install --upgrade pip
pip install -e "edge[yolo]"
if errorlevel 1 goto install_failed
:installed

if /I "%~1"=="--demo" goto demo
if exist "edge\configs\local.yaml" goto config_ok
copy "edge\configs\example.yaml" "edge\configs\local.yaml" >nul
echo Made edge\configs\local.yaml from example.yaml. Lines you draw are saved there.
:config_ok
if /I "%~1"=="--phone" goto phone
countvision-edge app --config edge\configs\local.yaml %*
goto end

:phone
countvision-edge app --config edge\configs\local.yaml --host 0.0.0.0 --preview blur
goto end

:demo
countvision-edge app --demo
goto end

:no_python
echo.
echo Python was not found. Install Python 3.10 or newer from https://www.python.org
echo and tick "Add python.exe to PATH" during the install. Then run start.bat again.
pause
exit /b 1

:install_failed
echo.
echo The install failed. Scroll up to see the error and send it to your engineer.
pause
exit /b 1

:end
endlocal
