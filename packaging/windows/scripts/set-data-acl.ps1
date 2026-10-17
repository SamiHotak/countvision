<#
.SYNOPSIS
  Protects the CountVision data folder (C:\ProgramData\CountVision).

.DESCRIPTION
  Run by the setup (as administrator). The data folder holds the database, its password, the secret
  key and the camera addresses. By default every Windows user may read new folders in ProgramData,
  so this script allows only:
    * SYSTEM and the Administrators group (backups, support, the uninstaller), and
    * the person who uses CountVision: the user logged in at this PC's screen. When an administrator
      account approved the setup for a normal user, that normal user is still the one at the screen.
  The folder's contents then inherit exactly these rights.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$User = ""
)
$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$owner = ""
try { $owner = (Get-CimInstance -ClassName Win32_ComputerSystem).UserName } catch { $owner = "" }
if (-not $owner) { $owner = $User }
if (-not $owner) { $owner = "$env:USERDOMAIN\$env:USERNAME" }

Write-Output "CountVision data folder: $DataDir, allowed: SYSTEM, Administrators, $owner"

# Only this folder: no inherited rights from ProgramData, our three entries for everything inside.
& icacls.exe $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "${owner}:(OI)(CI)F" /Q
if ($LASTEXITCODE -ne 0) { throw "icacls failed for $DataDir (exit code $LASTEXITCODE)" }

# What is already inside (an update, or a version that was run from the source code) follows the folder.
if (Get-ChildItem -LiteralPath $DataDir -Force | Select-Object -First 1) {
    & icacls.exe (Join-Path $DataDir "*") /reset /T /C /Q | Out-Null
}
exit 0
