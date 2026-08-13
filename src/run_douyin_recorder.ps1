$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$packRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
$candidates = @(
    (Join-Path $packRoot "_runtime\python\python.exe"),
    (Join-Path $PSScriptRoot "_runtime\python\python.exe"),
    "python.exe",
    "py.exe"
)

$python = $null
foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) {
        $python = $candidate
        break
    }
    $command = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($command) {
        $python = $command.Source
        break
    }
}

if (-not $python) {
    throw "python.exe was not found. The portable runtime is missing and no system Python was found."
}

& $python "$PSScriptRoot\douyin_live_watcher.py" --config "$PSScriptRoot\config.json"
