# CountVision - one-line installer for Windows 10/11 (pilot laptop or Windows mini-PC).
#
#   Open PowerShell (not as admin) and paste:
#   irm https://raw.githubusercontent.com/SamiHotak/countvision/main/install.ps1 | iex
#
# What it does (run it again to update; your settings and numbers are kept):
#   1. checks Python 3.10+ (offers to install Python 3.12 with winget if missing)
#   2. downloads CountVision to %LOCALAPPDATA%\CountVision\app
#   3. installs it in its own Python environment (.venv) and downloads the YOLOX-Tiny model
#   4. asks for the camera address (RTSP) or uses the USB webcam
#   5. creates desktop shortcuts and, if you want, starts counting automatically at login
#
# Options (set before running): $env:CV_DIR, $env:CV_BRANCH, $env:CV_YES='1' (no questions),
#   $env:CV_AUTOSTART='1', $env:CV_SOURCE='C:\path\to\checkout' (developers)
# Works with Windows PowerShell 5.1 and PowerShell 7.

& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is much faster without the bar

$Repo = if ($env:CV_REPO) { $env:CV_REPO } else { 'SamiHotak/countvision' }
$Branch = if ($env:CV_BRANCH) { $env:CV_BRANCH } else { 'main' }
$Dir = if ($env:CV_DIR) { $env:CV_DIR } else { Join-Path $env:LOCALAPPDATA 'CountVision' }
$App = Join-Path $Dir 'app'
$Yes = ($env:CV_YES -eq '1')

function Say($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Warn($text) { Write-Host "WARNING: $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "ERROR: $text" -ForegroundColor Red; throw $text }
function Ask($question, $default) {
    if ($Yes) { return $default }
    $answer = Read-Host $question
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
    return $answer.Trim()
}

function Find-Python {
    $candidates = @(
        @('py', '-3.12'), @('py', '-3.13'), @('py', '-3'), @('python'),
        @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))
    )
    foreach ($c in $candidates) {
        $exe = $c[0]
        $args1 = @()
        if ($c.Count -gt 1) { $args1 = $c[1..($c.Count - 1)] }
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue) -and -not (Test-Path $exe)) { continue }
        try {
            $ok = & $exe @args1 -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
            if ($ok -eq 'True') {
                $path = & $exe @args1 -c "import sys; print(sys.executable)"
                return $path.Trim()
            }
        } catch { continue }
    }
    return $null
}

Say "CountVision installer: $Dir (code: github.com/$Repo, branch $Branch)"

# ---------------------------------------------------------------- 1. Python
$Python = Find-Python
if (-not $Python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail 'Python 3.10+ not found. Install Python 3.12 from https://www.python.org (tick "Add python.exe to PATH") and run this again.'
    }
    $answer = Ask 'Python 3.10+ not found. Install Python 3.12 now with winget? [Y/n]' 'y'
    if ($answer -match '^(n|no)$') { Fail 'CountVision needs Python 3.10 or newer.' }
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $Python = Find-Python
    if (-not $Python) { Fail 'Python was installed but not found. Close PowerShell, open it again and run the installer again.' }
}
Say "Python: $Python"

# ---------------------------------------------------------------- 2. code
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("countvision-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $Tmp | Out-Null
try {
    if ($env:CV_SOURCE) {
        $Src = $env:CV_SOURCE
    } else {
        Say 'Downloading CountVision ...'
        $zip = Join-Path $Tmp 'countvision.zip'
        Invoke-WebRequest -UseBasicParsing -Uri "https://codeload.github.com/$Repo/zip/refs/heads/$Branch" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $Tmp -Force
        $Src = (Get-ChildItem -Path $Tmp -Directory | Select-Object -First 1).FullName
    }
    if (-not (Test-Path (Join-Path $Src 'edge\pyproject.toml'))) { Fail "Download looks wrong (no edge\pyproject.toml in $Src)." }
    # Copy over the old version. Files that are only on this PC (local config, data, models,
    # .venv, videos) are kept because robocopy /E never deletes.
    robocopy $Src $App /E /NFL /NDL /NJH /NJS /NP /XD .git .venv __pycache__ | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Copying the files failed (robocopy code $LASTEXITCODE)." }
    $global:LASTEXITCODE = 0
} finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------- 3. install
$Venv = Join-Path $App '.venv'
$VPy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $VPy)) {
    Say 'Creating the Python environment ...'
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail 'python -m venv failed.' }
}
Say 'Installing CountVision (first time a few minutes) ...'
& $VPy -m pip install --quiet --upgrade pip
& $VPy -m pip install --quiet -e "$App\edge[onnx]"
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed. Scroll up for the reason.' }
$Cli = Join-Path $Venv 'Scripts\countvision-edge.exe'
Push-Location $App
try {
    & $Cli models download yolox_tiny --dir models
    if ($LASTEXITCODE -ne 0) { Fail 'Model download failed.' }
    & $Cli demo --no-video --out (Join-Path $Dir 'selftest') | Select-Object -Last 1
} finally { Pop-Location }

# ---------------------------------------------------------------- 4. camera
$Config = Join-Path $App 'edge\configs\local.yaml'
if (Test-Path $Config) {
    Say "Keeping your config: $Config"
} else {
    Write-Host ''
    Write-Host 'Camera address (RTSP), e.g. rtsp://user:password@192.168.1.20:554/stream2'
    Write-Host 'Leave it empty to use the USB webcam of this PC.'
    $Url = Ask 'Camera:' ''
    $text = Get-Content -Raw -Path (Join-Path $App 'edge\configs\example.yaml')
    if ($Url) {
        # The password is stored as a Windows user variable, not in the config file.
        [Environment]::SetEnvironmentVariable('CAM1_URL', $Url, 'User')
        $env:CAM1_URL = $Url
        $text = $text -replace '(?m)^(\s+uri:)\s*0\b.*$', '$1 ${CAM1_URL}   # set by install.ps1 (Windows user variable CAM1_URL)'
        $text = $text -replace '(?m)^\s+(width|height|fourcc):.*\r?\n', ''
    }
    [System.IO.File]::WriteAllText($Config, $text, (New-Object System.Text.UTF8Encoding $false))  # UTF-8 without BOM
    Say "Config written: $Config"
}

# ---------------------------------------------------------------- 5. shortcuts and autostart
$Shell = New-Object -ComObject WScript.Shell
$Desktop = [Environment]::GetFolderPath('Desktop')
$links = @(
    @{ Name = 'CountVision'; Args = ''; Note = 'Web app: picture, draw lines, live numbers' },
    @{ Name = 'CountVision - count only'; Args = '--count'; Note = 'Counting without the web page' }
)
foreach ($l in $links) {
    $lnk = $Shell.CreateShortcut((Join-Path $Desktop ($l.Name + '.lnk')))
    $lnk.TargetPath = Join-Path $App 'start.bat'
    $lnk.Arguments = $l.Args
    $lnk.WorkingDirectory = $App
    $lnk.Description = $l.Note
    $lnk.Save()
}
Say 'Desktop shortcuts: "CountVision" and "CountVision - count only"'

$auto = if ($env:CV_AUTOSTART -eq '1') { 'y' } else { Ask 'Start counting automatically when you log in to Windows? [y/N]' 'n' }
if ($auto -match '^(y|yes|j|ja)$') {
    $action = New-ScheduledTaskAction -Execute (Join-Path $App 'start.bat') -Argument '--count' -WorkingDirectory $App
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName 'CountVision' -Action $action -Trigger $trigger -Settings $settings `
        -Description 'CountVision: count all cameras (start.bat --count)' -Force | Out-Null
    Say 'Autostart on: counting starts when you log in (Task Scheduler, task "CountVision").'
    Write-Host '   Remove it: Unregister-ScheduledTask -TaskName CountVision'
}

Write-Host ''
Say 'Done.'
Write-Host '  1. Double-click "CountVision" on the desktop: picture, draw the counting line, Save.'
Write-Host '  2. Close it, then double-click "CountVision - count only" (or log off/on with autostart).'
Write-Host '     Do not run both at the same time: then everything is counted twice.'
Write-Host "  Update later: run the same install command again. Folder: $App"
}
