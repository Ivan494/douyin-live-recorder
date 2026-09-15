[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallRoot
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$source = Join-Path $repo 'src'
$target = Join-Path ([IO.Path]::GetFullPath($InstallRoot)) 'douyindownload\_automation'

function Invoke-Git([string]$Directory, [string[]]$Arguments) {
    $result = & git -C $Directory @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Git failed: $($Arguments -join ' ')" }
    return $result
}

$revision = Invoke-Git $repo @('rev-parse', 'HEAD')
if (Invoke-Git $repo @('status', '--porcelain')) { throw 'Commit or stash source changes before deploying.' }
if (!(Test-Path -LiteralPath (Join-Path $target '.git'))) {
    throw 'The personal automation directory needs a source-only Git checkpoint repository first.'
}
if (Invoke-Git $target @('status', '--porcelain')) { throw 'Personal source has edits. Reconcile them in the source repository first.' }

# Deliberate allowlist: never copy JSON settings, sessions, logs, recordings,
# browser profiles, executables, dependencies, or the public .git directory.
$files = @('douyin_abogus.py', 'douyin_live_watcher.py', 'douyin_media_downloader.py',
    'douyin_recorder_app.py', 'i18n.py', 'recording_urls.py', 'security_utils.py', 'release_selftest.py')
$files += @(Get-ChildItem -LiteralPath (Join-Path $source 'tests') -Filter '*.py' -File |
    ForEach-Object { 'tests/' + $_.Name })
foreach ($file in $files) {
    if (!(Test-Path -LiteralPath (Join-Path $source $file))) { throw "Missing source: $file" }
}

if (!$PSCmdlet.ShouldProcess($target, "Deploy committed source $revision with local Git checkpoints")) { return }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$before = "checkpoint-before-deploy-$stamp"
$after = "checkpoint-after-deploy-$stamp"
Invoke-Git $target @('tag', $before) | Out-Null
foreach ($file in $files) {
    $destination = Join-Path $target $file
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $destination
    if ((Get-FileHash -LiteralPath $destination).Hash -ne (Get-FileHash -LiteralPath (Join-Path $source $file)).Hash) {
        throw "Verification failed: $file. Restore the local checkpoint $before before retrying."
    }
}
Invoke-Git $target (@('add', '--') + $files) | Out-Null
if (Invoke-Git $target @('diff', '--cached', '--name-only')) {
    Invoke-Git $target @('commit', '-m', "deploy: source $revision") | Out-Null
}
Invoke-Git $target @('tag', $after) | Out-Null
$record = [ordered]@{ sourceCommit=$revision; installedAt=(Get-Date).ToString('o'); beforeCheckpoint=$before; afterCheckpoint=$after; files=$files }
$record | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $target 'deployed-source.json') -Encoding UTF8
Write-Output "Deployed $revision. Checkpoint: $after"
Write-Output 'Restart the personal recorder when recording can safely be interrupted. Running processes keep their loaded code.'
