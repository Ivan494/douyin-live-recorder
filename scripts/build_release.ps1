param(
    [string]$Version = "1.0.0",
    [string]$FfmpegDir = "",
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root "src"
$dist = Join-Path $root "dist"
$staging = Join-Path $dist "DouyinLiveRecorder-v$Version-win64"

if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Force -Path (Join-Path $staging "douyindownload\_automation") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $staging "youtube-dl") | Out-Null

& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name DouyinLiveRecorder `
    --hidden-import pystray._win32 `
    --distpath (Join-Path $dist "exe") --workpath (Join-Path $dist "work") --specpath (Join-Path $dist "spec") `
    (Join-Path $src "douyin_recorder_app.py")

Copy-Item (Join-Path $dist "exe\DouyinLiveRecorder.exe") (Join-Path $staging "douyindownload\_automation")
Copy-Item (Join-Path $src "profiles.json") (Join-Path $staging "douyindownload\_automation")
Copy-Item (Join-Path $src "settings.json") (Join-Path $staging "douyindownload\_automation")

if (-not $FfmpegDir) {
    $candidates = @(
        (Join-Path $root "tools"),
        (Join-Path $root "..\youtube-dl"),
        (Join-Path $root "..\..\youtube-dl")
    )
    foreach ($c in $candidates) {
        if (Test-Path (Join-Path $c "ffmpeg.exe")) { $FfmpegDir = $c; break }
    }
}
if (-not $FfmpegDir -or -not (Test-Path (Join-Path $FfmpegDir "ffmpeg.exe"))) {
    throw "ffmpeg.exe not found. Pass -FfmpegDir pointing at a folder with ffmpeg.exe/ffprobe.exe."
}
Copy-Item (Join-Path $FfmpegDir "ffmpeg.exe") (Join-Path $staging "youtube-dl")
Copy-Item (Join-Path $FfmpegDir "ffprobe.exe") (Join-Path $staging "youtube-dl")

$zip = Join-Path $dist "DouyinLiveRecorder-v$Version-win64.zip"
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zip -CompressionLevel Optimal
Write-Host "Release zip: $zip"
