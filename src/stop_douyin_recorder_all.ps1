$patterns = @(
    (Join-Path $PSScriptRoot "douyin_live_watcher.py"),
    (Join-Path $PSScriptRoot "douyin_recorder_app.py")
)

$processes = Get-CimInstance Win32_Process | Where-Object {
    $cmd = $_.CommandLine
    $exe = $_.ExecutablePath
    ($exe -like "*\python.exe" -or $exe -like "*\pythonw.exe") -and
    ($patterns | Where-Object { $cmd -like "*$_*" })
}

foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

$packRoot = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")
$ffmpegPath = Join-Path $packRoot "youtube-dl\ffmpeg.exe"
$ffmpeg = Get-CimInstance Win32_Process |
    Where-Object { $_.ExecutablePath -eq $ffmpegPath -and $_.CommandLine -like "*douyindownload*" }

foreach ($process in $ffmpeg) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
