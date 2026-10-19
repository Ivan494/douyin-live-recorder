$ErrorActionPreference = 'Stop'

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'release_launcher.cs'
$extractAdvice = -join @([char]0x8BF7, [char]0x5B8C, [char]0x6574, [char]0x89E3, [char]0x538B)
$tempRoot = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ('douyin-launcher-test-' + [Guid]::NewGuid().ToString('N'))))
$safeRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
if (!$tempRoot.StartsWith($safeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create a launcher test directory outside the temp directory.'
}

function Invoke-Launcher([string]$Executable) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Executable
    $info.Arguments = '--self-test-report'
    $info.WorkingDirectory = $tempRoot
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardError = $true
    $info.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    $process = [Diagnostics.Process]::Start($info)
    try {
        if (!$process.WaitForExit(10000)) {
            $process.Kill()
            throw 'Launcher self-test timed out.'
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            ErrorText = $process.StandardError.ReadToEnd()
        }
    }
    finally {
        $process.Dispose()
    }
}

try {
    $childDir = Join-Path $tempRoot 'douyindownload\_automation'
    New-Item -ItemType Directory -Path $childDir -Force | Out-Null
    $launcher = Join-Path $tempRoot 'DouyinLiveRecorder.exe'
    & $compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll "/out:$launcher" $source
    if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed.' }

    $child = Join-Path $childDir 'DouyinLiveRecorder.exe'
    [IO.File]::WriteAllText($child, 'This file is deliberately not an executable.')
    Write-Output 'Checking existing invalid executable...'
    $blocked = Invoke-Launcher $launcher
    if ($blocked.ExitCode -ne 1 -or
        !$blocked.ErrorText.Contains($child) -or
        $blocked.ErrorText -notmatch '(?m)^Windows .*: \d+$' -or
        $blocked.ErrorText.Contains($extractAdvice)) {
        throw "The launcher concealed the actual Windows start error for an existing child executable. Exit=$($blocked.ExitCode); stderr=$($blocked.ErrorText)"
    }

    Remove-Item -LiteralPath $child -Force
    Write-Output 'Checking missing executable...'
    $missing = Invoke-Launcher $launcher
    if ($missing.ExitCode -ne 1 -or
        !$missing.ErrorText.Contains($child) -or
        !$missing.ErrorText.Contains($extractAdvice)) {
        throw "The launcher did not identify the genuinely missing child executable. Exit=$($missing.ExitCode); stderr=$($missing.ErrorText)"
    }

    $quickExitSource = Join-Path $tempRoot 'QuickExit.cs'
    [IO.File]::WriteAllText($quickExitSource, 'public static class QuickExit { public static int Main(string[] args) { return 23; } }')
    & $compiler /nologo /target:winexe /platform:x64 /optimize+ "/out:$child" $quickExitSource
    if ($LASTEXITCODE -ne 0) { throw 'Quick-exit test executable compilation failed.' }
    Write-Output 'Checking immediate child failure...'
    $quickExit = Invoke-Launcher $launcher
    if ($quickExit.ExitCode -ne 1 -or
        !$quickExit.ErrorText.Contains('23') -or
        !$quickExit.ErrorText.Contains($child) -or
        $quickExit.ErrorText.Contains($extractAdvice)) {
        throw "The launcher concealed an immediate child exit code. Exit=$($quickExit.ExitCode); stderr=$($quickExit.ErrorText)"
    }

    Write-Output 'PASS: launcher reports Windows start failures, missing files, and immediate child exits.'
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        $resolved = [IO.Path]::GetFullPath($tempRoot)
        if (!$resolved.StartsWith($safeRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Refusing to remove a launcher test directory outside the temp directory.'
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
