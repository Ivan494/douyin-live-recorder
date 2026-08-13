$scriptPath = Join-Path $PSScriptRoot "douyin_live_watcher.py"
$watchers = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*$scriptPath*" }

foreach ($watcher in $watchers) {
    Stop-Process -Id $watcher.ProcessId -Force -ErrorAction SilentlyContinue
}

$packRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
$ffmpegPath = Join-Path $packRoot "youtube-dl\ffmpeg.exe"
$ffmpeg = Get-CimInstance Win32_Process |
    Where-Object { $_.ExecutablePath -eq $ffmpegPath -and $_.CommandLine -like "*douyindownload*" }

foreach ($process in $ffmpeg) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
