<#
.SYNOPSIS
  Stops a running CountVision Local before an update or the uninstall.

.DESCRIPTION
  1. Asks the running CountVision to quit cleanly (the counting service saves its last minute, the
     database stops last). Works for every Windows user's instance: it uses the lock file in the
     data folder, which administrators may read.
  2. Stops the bundled database with pg_ctl if it is still running.
  3. Ends whatever is still running from the program folder (python, node, postgres).
  Never fails: the setup must be able to continue.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [string]$DataDir = (Join-Path $env:ProgramData "CountVision"),
    [int]$TimeoutSeconds = 120
)
$ErrorActionPreference = "Continue"
$AppDir = $AppDir.TrimEnd("\")

function Get-AppProcesses {
    Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith("$AppDir\", [StringComparison]::OrdinalIgnoreCase) }
}

$python = Join-Path $AppDir "python\python.exe"
$lock = Join-Path $DataDir "run\app.lock"
if ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $lock)) {
    Write-Output "Asking CountVision to stop ..."
    $job = Start-Process -FilePath $python -ArgumentList @("-m", "countvision_desktop", "--quit", "--home", "`"$DataDir`"") `
        -WindowStyle Hidden -PassThru
    if (-not $job.WaitForExit($TimeoutSeconds * 1000)) {
        Write-Output "CountVision did not stop within $TimeoutSeconds s"
        $job.Kill()
    }
}

$pgCtl = Join-Path $AppDir "pgsql\bin\pg_ctl.exe"
$pgData = Join-Path $DataDir "db"
if ((Test-Path -LiteralPath $pgCtl) -and (Test-Path -LiteralPath (Join-Path $pgData "postmaster.pid"))) {
    Write-Output "Stopping the database ..."
    & $pgCtl stop -D $pgData -m fast -w -t 30 2>&1 | Out-Null
}

$left = @(Get-AppProcesses)
foreach ($proc in $left) {
    Write-Output "Ending $($proc.Name) (pid $($proc.ProcessId))"
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($left.Count -gt 0) { Start-Sleep -Seconds 2 }
exit 0
