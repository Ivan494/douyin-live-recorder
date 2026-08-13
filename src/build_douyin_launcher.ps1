$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$sourcePath = Join-Path $PSScriptRoot "DouyinLiveRecorderLauncher.cs"
$outputPath = Join-Path $PSScriptRoot "DouyinLiveRecorder.exe"
$appScriptPath = Join-Path $PSScriptRoot "douyin_recorder_app.py"

if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "Missing launcher source: $sourcePath"
}

$running = Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -eq $outputPath -or
    ($_.CommandLine -and $_.CommandLine -like "*$appScriptPath*")
}

if ($running) {
    $processList = ($running | ForEach-Object { "$($_.Name) PID $($_.ProcessId)" }) -join ", "
    throw "The recorder is running ($processList). Close it only after all live recordings finish, then rebuild the launcher."
}

$source = Get-Content -LiteralPath $sourcePath -Raw
Remove-Item -LiteralPath $outputPath -Force -ErrorAction SilentlyContinue
Add-Type -TypeDefinition $source -OutputAssembly $outputPath -OutputType WindowsApplication

Get-Item -LiteralPath $outputPath
