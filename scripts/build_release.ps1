param(
    [string]$Version = "1.2.4",
    [Parameter(Mandatory = $true)][string]$FfmpegDir,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$Version = $Version.TrimStart("vV")
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must be X.Y.Z.' }
$root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$src = Join-Path $root 'src'
$dist = Join-Path $root 'dist'
$staging = Join-Path $dist "DouyinLiveRecorder-v$Version-win64"
$work = Join-Path $dist "build-v$Version"

function Invoke-Python([string[]]$Arguments) {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python build/check failed (exit $LASTEXITCODE)." }
}

# Validate inputs before touching any output, and never remove the whole dist tree.
Invoke-Python @((Join-Path $PSScriptRoot 'release_artifacts.py'), '--source')
foreach ($tool in @('ffmpeg.exe', 'ffprobe.exe')) {
    if (!(Test-Path -LiteralPath (Join-Path $FfmpegDir $tool) -PathType Leaf)) { throw "Missing $tool" }
}
foreach ($directory in @($staging, $work)) {
    $resolved = [IO.Path]::GetFullPath($directory)
    if (!(($resolved + '\').StartsWith($dist + '\', [StringComparison]::OrdinalIgnoreCase))) {
        throw 'Refusing cleanup outside the release output directory.'
    }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
$app = Join-Path $staging 'douyindownload\_automation'
New-Item -ItemType Directory -Force -Path $app, (Join-Path $staging 'youtube-dl') | Out-Null

Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', 'DouyinLiveRecorder', '--paths', $src,
    '--hidden-import', 'pystray._win32', '--collect-submodules', 'signer',
    '--collect-data', 'streamget', '--recursive-copy-metadata', 'streamget',
    '--distpath', (Join-Path $work 'exe'), '--workpath', (Join-Path $work 'work'),
    '--specpath', (Join-Path $work 'spec'), (Join-Path $src 'douyin_recorder_app.py'))
$executable = Join-Path $work 'exe\DouyinLiveRecorder.exe'
if (!(Test-Path -LiteralPath $executable)) { throw 'PyInstaller produced no executable.' }
Invoke-Python @((Join-Path $PSScriptRoot 'release_artifacts.py'), '--exe', $executable)
Copy-Item -LiteralPath $executable -Destination $app

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll `
    "/out:$staging\DouyinLiveRecorder.exe" (Join-Path $PSScriptRoot 'release_launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Root launcher compilation failed.' }

# Exact allowlist. Never copy a source/installation directory recursively.
Copy-Item -LiteralPath (Join-Path $src 'profiles.json'), (Join-Path $src 'settings.json') -Destination $app
Copy-Item -LiteralPath (Join-Path $FfmpegDir 'ffmpeg.exe'), (Join-Path $FfmpegDir 'ffprobe.exe') -Destination (Join-Path $staging 'youtube-dl')
Copy-Item -LiteralPath (Join-Path $root 'LICENSE') -Destination $staging
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'RELEASE_README.txt') -Destination (Join-Path $staging 'README.txt')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD_PARTY.md') -Destination $staging
Invoke-Python @((Join-Path $PSScriptRoot 'release_artifacts.py'), '--stage', $staging, '--version', $Version)

$zip = Join-Path $dist "DouyinLiveRecorder-v$Version-win64.zip"
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zip -CompressionLevel Optimal -Force
Invoke-Python @((Join-Path $PSScriptRoot 'release_artifacts.py'), '--zip', $zip, '--smoke')
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $([IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii
Write-Output "Validated release: $zip"
