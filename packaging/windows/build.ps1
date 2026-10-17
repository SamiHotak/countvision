<#
.SYNOPSIS
  Builds the CountVision Local setup: dist\CountVision-Local-Setup-<version>.exe

.DESCRIPTION
  Runs on Windows 10/11 x64 (GitHub Actions windows-latest, or a developer PC) and needs:
    * Python 3.12 (64-bit) as "python" - only to install the packages; the setup brings its own Python
    * Node.js 20 or 22 as "node"/"npm" - builds the app; its node.exe is copied into the setup
    * internet (downloads below, ~250 MB the first time; cached in build\windows\downloads)
    * Inno Setup 6 (installed with Chocolatey when missing)

  Steps:
    1. embeddable Python 3.12 (python.org) + all packages: counting engine (edge[onnx]), API
       (cloud/api), desktop app (desktop[window]), compiled to .pyc
    2. YOLOX-Tiny model (checksum checked)
    3. the app (Next.js standalone build) + node.exe
    4. PostgreSQL 16 binaries (EDB zip; bin, lib, share only)
    5. CountVision.exe (compiled from launcher\CountVision.cs with the C# compiler of Windows)
    6. Microsoft Visual C++ runtime + WebView2 bootstrapper (signature checked), for the setup
    7. checks (imports, "countvision-desktop --check") and Inno Setup

  Everything in it is free: Python (PSF), Node.js (MIT), PostgreSQL (PostgreSQL licence),
  YOLOX (Apache-2.0), Inno Setup (free), Microsoft runtimes (free to redistribute).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12.10",     # the last 3.12 with a Windows embeddable zip
    [string]$PostgresVersion = "16.15-1",   # EDB binaries zip name; newest 16.x on 2026-10-17
    [string]$Work = "build\windows",
    [string]$Out = "dist",
    [switch]$SkipWebBuild,                  # reuse cloud\web\.next (already built)
    [switch]$SkipInstaller                  # only the stage folder (faster while testing)
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is many times faster without the bar
Set-StrictMode -Version 3

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$Work = Join-Path $Root $Work
$Out = Join-Path $Root $Out
$Stage = Join-Path $Work "stage"
$Downloads = Join-Path $Work "downloads"
$Redist = Join-Path $Work "redist"
$started = Get-Date

function Step([string]$text) { Write-Host "`n=== $text" -ForegroundColor Cyan }

function Invoke-Checked([string]$what, [scriptblock]$block) {
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" }
}

function Get-Download([string]$url, [string]$name) {
    New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
    $target = Join-Path $Downloads $name
    if (Test-Path -LiteralPath $target) { return $target }
    for ($try = 1; $try -le 4; $try++) {
        try {
            Write-Host "Downloading $url"
            Invoke-WebRequest -Uri $url -OutFile "$target.part" -UseBasicParsing
            Move-Item -LiteralPath "$target.part" -Destination $target -Force
            return $target
        } catch {
            Write-Warning "Download failed ($try/4): $($_.Exception.Message)"
            Start-Sleep -Seconds (5 * $try)
        }
    }
    throw "Could not download $url"
}

function Expand-Zip([string]$zip, [string]$dest) {
    # Windows' own tar.exe unpacks zip files many times faster than Expand-Archive (PowerShell 5.1).
    # (Not the tar of Git for Windows, which cannot read zip files.)
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $tar = Join-Path $env:WINDIR "System32\tar.exe"
    if (Test-Path -LiteralPath $tar) {
        & $tar -xf $zip -C $dest
        if ($LASTEXITCODE -eq 0) { return }
        Write-Warning "tar.exe could not unpack $zip, using Expand-Archive"
    }
    Expand-Archive -LiteralPath $zip -DestinationPath $dest -Force
}

function Assert-MicrosoftSigned([string]$file) {
    $sig = Get-AuthenticodeSignature -FilePath $file
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
        throw "$file is not signed by Microsoft ($($sig.Status))"
    }
}

function Get-Size([string]$path) {
    $bytes = (Get-ChildItem -LiteralPath $path -Recurse -File | Measure-Object -Property Length -Sum).Sum
    return "{0:N0} MB" -f ($bytes / 1MB)
}

# --- version -------------------------------------------------------------------------------------
$Version = (Select-String -Path "desktop\pyproject.toml" -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
$Commit = "local"
try { $Commit = (& git rev-parse --short HEAD 2>$null).Trim() } catch { }
if (-not $Commit) { $Commit = "local" }
Write-Host "CountVision Local $Version ($Commit)"

# --- 0. tools ------------------------------------------------------------------------------------
Step "Checking the build tools"
$pyInfo = & python -c "import struct, sys; print('%d.%d %d' % (sys.version_info[0], sys.version_info[1], struct.calcsize('P') * 8))"
if ($pyInfo -ne "3.12 64") { throw "Python 3.12 64-bit is needed as 'python' (found: $pyInfo)" }
$nodeExe = (Get-Command node -ErrorAction Stop).Source
$nodeVersion = (& node --version).Trim()
if ($nodeVersion -notmatch '^v(20|22|24)\.') { throw "Node.js 20, 22 or 24 is needed (found $nodeVersion)" }
Write-Host "Python $pyInfo, Node.js $nodeVersion ($nodeExe)"

if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Stage, $Redist, $Out | Out-Null

# --- 1. Python ----------------------------------------------------------------------------------
Step "Python $PythonVersion (embeddable) + packages"
$pyZip = Get-Download "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" `
    "python-$PythonVersion-embed-amd64.zip"
$pyDir = Join-Path $Stage "python"
Expand-Zip $pyZip $pyDir
$site = Join-Path $pyDir "Lib\site-packages"
New-Item -ItemType Directory -Force -Path $site | Out-Null
# The ._pth file decides what this Python sees: its own files only (no PYTHONPATH, no user site).
$pth = Get-ChildItem -LiteralPath $pyDir -Filter "python3*._pth" | Select-Object -First 1
$zipName = (Get-ChildItem -LiteralPath $pyDir -Filter "python3*.zip" | Select-Object -First 1).Name
Set-Content -LiteralPath $pth.FullName -Encoding Ascii -Value @($zipName, ".", "Lib\site-packages", "import site")

Invoke-Checked "pip install" {
    python -m pip install --disable-pip-version-check --no-warn-script-location --no-compile `
        --target $site "$Root\edge[onnx]" "$Root\cloud\api" "$Root\desktop[window]"
}
# console scripts made by pip point at the build machine's Python: not needed (CountVision.exe runs -m)
$bin = Join-Path $site "bin"
if (Test-Path -LiteralPath $bin) { Remove-Item -LiteralPath $bin -Recurse -Force }
Get-ChildItem -LiteralPath $site -Directory -Filter "__pycache__" -Recurse | Remove-Item -Recurse -Force
# Program Files is read-only for normal users: compile everything now (fast first start)
& "$pyDir\python.exe" -m compileall -q -j 0 $site | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warning "compileall reported files it could not compile (test files of packages)" }
Invoke-Checked "import check" {
    & "$pyDir\python.exe" -c "import countvision_cloud.cli, countvision_cloud.local.backup, countvision_edge.supervisor, countvision_desktop.launcher, cv2, onnxruntime, psycopg, webview, pystray, PIL, clr; print('imports ok')"
}

# --- 2. model -----------------------------------------------------------------------------------
Step "Detector model (YOLOX-Tiny, Apache-2.0)"
Invoke-Checked "model download" { & "$pyDir\python.exe" -m countvision_edge models download --dir (Join-Path $Stage "models") }

# --- 3. app -------------------------------------------------------------------------------------
Step "The app (Next.js standalone) + node.exe"
$webSrc = Join-Path $Root "cloud\web"
if (-not $SkipWebBuild) {
    Push-Location $webSrc
    try {
        $env:NEXT_TELEMETRY_DISABLED = "1"
        $env:API_URL = "http://127.0.0.1:8001"   # the desktop app's API port (built into the app)
        Invoke-Checked "npm ci" { npm ci --no-audit --no-fund }
        Invoke-Checked "npm run build" { npm run build }
    } finally {
        Remove-Item Env:\API_URL -ErrorAction SilentlyContinue
        Pop-Location
    }
}
$standalone = Join-Path $webSrc ".next\standalone"
$server = Get-ChildItem -LiteralPath $standalone -Recurse -Filter "server.js" -File |
    Where-Object { $_.FullName -notmatch '\\node_modules\\' } | Sort-Object { $_.FullName.Length } | Select-Object -First 1
if (-not $server) { throw "No server.js in $standalone (is output: 'standalone' set in next.config.ts?)" }
$webDir = Join-Path $Stage "web"
Copy-Item -LiteralPath $server.Directory.FullName -Destination $webDir -Recurse
Copy-Item -LiteralPath (Join-Path $webSrc ".next\static") -Destination (Join-Path $webDir ".next\static") -Recurse
if (Test-Path -LiteralPath (Join-Path $webSrc "public")) {
    Copy-Item -LiteralPath (Join-Path $webSrc "public") -Destination (Join-Path $webDir "public") -Recurse
}
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "node") | Out-Null
Copy-Item -LiteralPath $nodeExe -Destination (Join-Path $Stage "node\node.exe")
$nodeLicence = Join-Path (Split-Path $nodeExe) "LICENSE"
if (Test-Path -LiteralPath $nodeLicence) { Copy-Item -LiteralPath $nodeLicence -Destination (Join-Path $Stage "node\LICENSE.txt") }

# --- 4. PostgreSQL ------------------------------------------------------------------------------
Step "PostgreSQL $PostgresVersion"
$pgZip = Get-Download "https://get.enterprisedb.com/postgresql/postgresql-$PostgresVersion-windows-x64-binaries.zip" `
    "postgresql-$PostgresVersion-windows-x64-binaries.zip"
$pgTmp = Join-Path $Work "pgsql-unpacked"
if (Test-Path -LiteralPath $pgTmp) { Remove-Item -LiteralPath $pgTmp -Recurse -Force }
Expand-Zip $pgZip $pgTmp
$pgDst = Join-Path $Stage "pgsql"
New-Item -ItemType Directory -Force -Path $pgDst | Out-Null
foreach ($part in @("bin", "lib", "share")) {
    Copy-Item -LiteralPath (Join-Path $pgTmp "pgsql\$part") -Destination (Join-Path $pgDst $part) -Recurse
}
# development files and debugging symbols are not needed to run it
Get-ChildItem -LiteralPath $pgDst -Recurse -File -Include "*.lib", "*.pdb", "*.a" | Remove-Item -Force
foreach ($extra in @("bin\stackbuilder.exe", "bin\pgAdmin4.exe")) {
    $path = Join-Path $pgDst $extra
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}
Remove-Item -LiteralPath $pgTmp -Recurse -Force
Invoke-Checked "postgres --version" { & (Join-Path $pgDst "bin\postgres.exe") --version }

# --- 5. CountVision.exe -------------------------------------------------------------------------
Step "CountVision.exe"
$icon = Join-Path $Root "desktop\countvision_desktop\assets\icon.ico"
Copy-Item -LiteralPath $icon -Destination (Join-Path $Stage "CountVision.ico")
$numeric = ($Version -replace '[^0-9.]', '')
while (($numeric.Split('.')).Count -lt 4) { $numeric += ".0" }
$assemblyInfo = Join-Path $Work "AssemblyInfo.cs"
Set-Content -LiteralPath $assemblyInfo -Encoding Ascii -Value @(
    "using System.Reflection;",
    "[assembly: AssemblyTitle(""CountVision"")]",
    "[assembly: AssemblyDescription(""CountVision Local - visitor and traffic counting"")]",
    "[assembly: AssemblyProduct(""CountVision"")]",
    "[assembly: AssemblyCompany(""Sami Hotak"")]",
    "[assembly: AssemblyCopyright(""AGPL-3.0-or-later"")]",
    "[assembly: AssemblyVersion(""$numeric"")]",
    "[assembly: AssemblyFileVersion(""$numeric"")]",
    "[assembly: AssemblyInformationalVersion(""$Version+$Commit"")]"
)
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $csc)) { throw "The C# compiler of the .NET Framework was not found: $csc" }
Invoke-Checked "csc" {
    & $csc /nologo /target:winexe /optimize+ /platform:anycpu "/out:$(Join-Path $Stage 'CountVision.exe')" `
        "/win32icon:$icon" /reference:System.Windows.Forms.dll `
        (Join-Path $PSScriptRoot "launcher\CountVision.cs") $assemblyInfo
}

# --- 6. tools, texts, version -------------------------------------------------------------------
$tools = Join-Path $Stage "tools"
New-Item -ItemType Directory -Force -Path $tools | Out-Null
Copy-Item -Path (Join-Path $PSScriptRoot "scripts\*.ps1") -Destination $tools
Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination (Join-Path $Stage "LICENSE.txt")
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "THIRD-PARTY-NOTICES.txt") -Destination $Stage
Set-Content -LiteralPath (Join-Path $Stage "version.txt") -Encoding Ascii -Value "$Version+$Commit"

# --- 7. Microsoft runtimes for the setup ---------------------------------------------------------
Step "Microsoft Visual C++ runtime + WebView2 bootstrapper"
$vc = Get-Download "https://aka.ms/vs/17/release/vc_redist.x64.exe" "vc_redist.x64.exe"
$wv = Get-Download "https://go.microsoft.com/fwlink/p/?LinkId=2124703" "MicrosoftEdgeWebview2Setup.exe"
foreach ($file in @($vc, $wv)) {
    Assert-MicrosoftSigned $file
    Copy-Item -LiteralPath $file -Destination $Redist -Force
}

# --- 8. check -----------------------------------------------------------------------------------
Step "Checking the program folder"
$checkHome = Join-Path $Work "check-home"
if (Test-Path -LiteralPath $checkHome) { Remove-Item -LiteralPath $checkHome -Recurse -Force }
Invoke-Checked "countvision-desktop --check" {
    & "$pyDir\python.exe" -m countvision_desktop --check --home $checkHome
}
Write-Host "Program folder: $(Get-Size $Stage)"

if ($SkipInstaller) { Write-Host "Stage ready: $Stage"; exit 0 }

# --- 9. Inno Setup ------------------------------------------------------------------------------
Step "Inno Setup"
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) {
    $found = Get-Command iscc -ErrorAction SilentlyContinue
    if ($found) { $iscc = $found.Source }
}
if (-not $iscc) {
    Write-Host "Inno Setup not found: installing it with Chocolatey"
    Invoke-Checked "choco install innosetup" { choco install innosetup -y --no-progress }
    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
}
Invoke-Checked "ISCC" {
    & $iscc /Qp "/DAppVersion=$Version" "/DStageDir=$Stage" "/DRedistDir=$Redist" "/DOutputDir=$Out" `
        (Join-Path $PSScriptRoot "CountVision.iss")
}
$setup = Join-Path $Out "CountVision-Local-Setup-$Version.exe"
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLower()
Set-Content -LiteralPath "$setup.sha256" -Encoding Ascii -Value "$hash  CountVision-Local-Setup-$Version.exe"
$minutes = [int]((Get-Date) - $started).TotalMinutes
Write-Host ("`nDone in {0} min: {1} ({2:N0} MB)`nSHA-256 {3}" -f $minutes, $setup, ((Get-Item $setup).Length / 1MB), $hash) -ForegroundColor Green
