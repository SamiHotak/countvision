# CountVision Local - Windows setup (setup.exe)

`build.ps1` turns this repository into **one file** for shop PCs:
`dist\CountVision-Local-Setup-<version>.exe` (about 300-450 MB). The shop runs it, clicks Next, and
gets the **CountVision** desktop app: an icon on the desktop and in the Start menu, its own window,
autostart at logon, everything working without internet.

You normally do **not** build it yourself: GitHub builds it on every push (job `windows-installer`)
and keeps it for 14 days. Open the run on GitHub -> **Artifacts** -> `CountVision-Local-Setup` ->
download -> unzip -> `CountVision-Local-Setup-<version>.exe`.

## What is inside

| Part | Where (after install) | Licence |
|---|---|---|
| CountVision.exe (starts the app without a console window) | `C:\Program Files\CountVision\` | AGPL-3.0 (ours) |
| Python 3.12 (embeddable) + all packages, compiled | `python\` | PSF + each package's own |
| PostgreSQL 16 (bin, lib, share) | `pgsql\` | PostgreSQL licence |
| Node.js (`node.exe`) + the app (Next.js standalone) | `node\`, `web\` | MIT |
| YOLOX-Tiny detector | `models\yolox_tiny.onnx` | Apache-2.0 |
| scripts: data folder rights, clean stop | `tools\` | ours |
| **Data**: database, settings, logs, backups | `C:\ProgramData\CountVision\` | - |

The setup also installs, only when missing: the Microsoft Visual C++ runtime (inside the setup) and
Microsoft Edge WebView2 (downloaded; part of Windows 11 and current Windows 10. Without it and
without internet, CountVision opens in Microsoft Edge's app mode instead - also its own window).

## What the setup does

1. **Update?** A running CountVision is stopped cleanly first (`tools\stop-countvision.ps1`: the
   counting service saves its last minute, the database stops). Old program files are removed.
2. Copies the program, makes the icons (Start menu, desktop if ticked) and the **autostart** at
   logon (ticked by default): `CountVision.exe --hidden` = only the tray icon next to the clock,
   counting starts by itself. Not a Windows service on purpose: services cannot use USB webcams.
3. **Protects the data folder** `C:\ProgramData\CountVision` (`tools\set-data-acl.ps1`): only SYSTEM,
   Administrators and the person logged in at the screen may open it (it holds the database password,
   the secret key and camera passwords).
4. Adds a firewall rule "CountVision app (shop network)": private networks, port 3000, `node.exe`
   only. It does nothing until someone ticks "Allow phones and other PCs in this network" in the tray
   menu (until then the app listens on this PC only), but then Windows does not ask for an
   administrator.
5. The first start of CountVision (the desktop app, `desktop\`) creates the database (`initdb`),
   then shows the setup wizard. **After an update** the first start makes a full backup
   (`data\backups\before-update-....cvbackup`) **before** it updates the database. If that backup
   fails, the update is not applied (the start screen says so; Try again).

**Uninstall** (Windows Settings -> Apps -> CountVision): stops CountVision, removes the program,
the icons, the autostart and the firewall rule, then **asks** whether to delete the data too
(default: keep). Silent uninstall keeps the data unless `/DELETEDATA=1` is given.

Silent install (for IT people): `CountVision-Local-Setup-<version>.exe /VERYSILENT /SUPPRESSMSGBOXES
/NORESTART /TASKS=desktopicon,autostart`.

## Build it yourself (optional)

Needs Windows 10/11 x64, Python 3.12 64-bit (`python`), Node.js 22 (`node`, `npm`), internet, and
Inno Setup 6 (installed with Chocolatey when missing, or from https://jrsoftware.org).

```bat
cd C:\Users\hotak\Desktop\projects\countvision
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

* `-SkipInstaller`: only the program folder `build\windows\stage` (test it with
  `build\windows\stage\CountVision.exe`).
* `-SkipWebBuild`: reuse an existing `cloud\web\.next` build.
* Downloads are cached in `build\windows\downloads` (Python, PostgreSQL, Microsoft runtimes).
* Your laptop has Python 3.14 in `.venv`: the build needs a separate Python **3.12**
  (`winget install Python.Python.3.12`, then run the build in a new window where `python --version`
  shows 3.12).

## Not included / honest limits

* **No code signing** (a certificate costs about 100-300 EUR per year). Windows SmartScreen shows
  "Windows protected your PC" -> **More info** -> **Run anyway**. Buy one before selling widely.
* Windows 10 version 1809 or newer, 64-bit only.
* The setup does not include a database server for other PCs: one CountVision per shop PC.
* Annotated videos (video analysis) use Windows' own H.264 encoder (Media Foundation). On Windows
  "N" editions without the Media Feature Pack they are saved as MP4V (download only, may not play in
  the app).

## Files

* `build.ps1` - the build (see the comment at the top)
* `CountVision.iss` - the Inno Setup script (English + German texts)
* `launcher\CountVision.cs` - CountVision.exe (compiled with the C# compiler that comes with Windows)
* `scripts\set-data-acl.ps1`, `scripts\stop-countvision.ps1` - copied to `tools\`
* `smoke_test.py` - what CI checks against the installed program
* `THIRD-PARTY-NOTICES.txt` - licences of everything inside
