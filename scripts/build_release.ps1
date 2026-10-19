param(
    [string]$Version = "1.2.9",
    [Parameter(Mandatory = $true)][string]$FfmpegDir,
    [string]$Python = "python",
    [string]$SignCommand,
    [switch]$RequireSignatures
)
$ErrorActionPreference = "Stop"
$Version = $Version.TrimStart("vV")
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Version must be X.Y.Z.' }
if ($SignCommand) {
    $SignCommand = [IO.Path]::GetFullPath($SignCommand)
    if (!(Test-Path -LiteralPath $SignCommand -PathType Leaf)) { throw "Missing signing command: $SignCommand" }
    $RequireSignatures = $true
} elseif ($RequireSignatures) {
    throw 'A signing-required release needs -SignCommand.'
}
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

Invoke-Python @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
    '--name', 'DouyinLiveRecorder', '--paths', $src,
    '--hidden-import', 'pystray._win32', '--collect-submodules', 'signer',
    '--collect-data', 'streamget', '--recursive-copy-metadata', 'streamget',
    '--contents-directory', '_internal',
    '--distpath', (Join-Path $work 'exe'), '--workpath', (Join-Path $work 'work'),
    '--specpath', (Join-Path $work 'spec'), (Join-Path $src 'douyin_recorder_app.py'))
$bundle = Join-Path $work 'exe\DouyinLiveRecorder'
$executable = Join-Path $bundle 'DouyinLiveRecorder.exe'
if (!(Test-Path -LiteralPath $executable)) { throw 'PyInstaller produced no executable.' }
Invoke-Python @((Join-Path $PSScriptRoot 'release_artifacts.py'), '--exe', $executable)
# pip may add environment-specific REQUESTED markers to copied distribution
# metadata. They are install records, not runtime data; omit them before the
# reviewed bundle inventory is checked and packaged.
$internal = Join-Path $bundle '_internal'
foreach ($metadata in Get-ChildItem -LiteralPath $internal -Directory -Filter '*.dist-info') {
    $requested = Join-Path $metadata.FullName 'REQUESTED'
    if (Test-Path -LiteralPath $requested -PathType Leaf) {
        Remove-Item -LiteralPath $requested -Force
    }
}
foreach ($item in Get-ChildItem -LiteralPath $bundle -Force) {
    Copy-Item -LiteralPath $item.FullName -Destination $app -Recurse -Force
}

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
if ($RequireSignatures) {
    $listing = & $Python (Join-Path $PSScriptRoot 'release_artifacts.py') --list-pe $staging
    if ($LASTEXITCODE -ne 0) { throw 'Could not list staged native binaries for signing.' }
    $nativeFiles = @(ConvertFrom-Json -InputObject ($listing -join "`n"))
    if ($nativeFiles.Count -eq 0) { throw 'No staged native binaries to sign.' }
    foreach ($file in $nativeFiles) {
        & $SignCommand $file
        if ($LASTEXITCODE -ne 0) { throw "Signing failed for $file (exit $LASTEXITCODE)." }
    }
}
$signatureFlag = @()
if ($RequireSignatures) { $signatureFlag = @('--require-signatures') }
Invoke-Python (@((Join-Path $PSScriptRoot 'release_artifacts.py'), '--stage', $staging, '--bundle', $bundle, '--version', $Version) + $signatureFlag)

$zip = Join-Path $dist "DouyinLiveRecorder-v$Version-win64.zip"
Compress-Archive -Path (Join-Path $staging '*') -DestinationPath $zip -CompressionLevel Optimal -Force
Invoke-Python (@((Join-Path $PSScriptRoot 'release_artifacts.py'), '--zip', $zip, '--smoke') + $signatureFlag)
$hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
"$hash  $([IO.Path]::GetFileName($zip))" | Set-Content -LiteralPath (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii
Write-Output "Validated release: $zip"
