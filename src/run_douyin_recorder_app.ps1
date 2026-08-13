$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$packRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")

# Single launch path: prefer pythonw (no console), fall back to python.
$candidates = @(
    (Join-Path $packRoot "_runtime\python\pythonw.exe"),
    (Join-Path $PSScriptRoot "_runtime\python\pythonw.exe"),
    (Join-Path $packRoot "_runtime\python\python.exe"),
    (Join-Path $PSScriptRoot "_runtime\python\python.exe")
)

$runner = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        $runner = $candidate
        break
    }
}

if (-not $runner) {
    throw "pythonw/python was not found under the portable runtime."
}

# If a stale lock exists with a dead PID, the app clears it itself.
# Second start signals the running instance to show the window.
Start-Process -FilePath $runner -ArgumentList "`"$PSScriptRoot\douyin_recorder_app.py`"" -WorkingDirectory $PSScriptRoot -WindowStyle Normal
