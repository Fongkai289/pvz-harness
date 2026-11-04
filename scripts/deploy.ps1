<#
Deploy from Windows: pack the working tree, upload it, and let scripts/deploy_remote.sh replace the
code on the server (keeping .env, .venv, runs, logs, data and the server's uv.lock), sync deps,
optionally run the tests there, and restart the server in tmux session "pvz".

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\deploy.ps1 [-Test]

PowerShell 5.1 pipes text, not bytes, and turns here-strings into CRLF, so the tarball and the
remote script go up as files with scp instead of through a pipe.
#>
param(
    [switch]$Test,
    [string]$SshHost = "operator@server.example.com",
    [int]$Port = 22,
    [string]$Key = (Join-Path $PSScriptRoot "..\..\..\pvz_operator"),
    [string]$RemoteDir = "/srv/pvz-harness"
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Key = (Resolve-Path $Key).Path
$ssh = @("-n", "-i", $Key, "-p", $Port, "-o", "BatchMode=yes", "-o", "LogLevel=ERROR")
$scp = @("-q", "-i", $Key, "-P", $Port, "-o", "BatchMode=yes", "-o", "LogLevel=ERROR")
$remoteTar = "$RemoteDir/data/.pvz-deploy.tgz"
$remoteScript = "$RemoteDir/data/.pvz-deploy.sh"

function Invoke-Native([string]$exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

$stamp = (git -C $root rev-parse --short HEAD).Trim()
if (git -C $root status --porcelain) { $stamp += "-dirty" }
Write-Host "deploying $stamp from $root"

$tarball = Join-Path ([IO.Path]::GetTempPath()) "pvz-deploy-$PID.tgz"
try {
    Invoke-Native "$env:SystemRoot\System32\tar.exe" @(
        "-czf", $tarball,
        "--exclude", "./.env", "--exclude", "./.venv", "--exclude", "./runs", "--exclude", "./logs",
        "--exclude", "./data", "--exclude", "./.git", "--exclude", "./uv.lock", "--exclude", "./.cache",
        "--exclude", "./.venv-sim",
        "--exclude", "__pycache__", "--exclude", ".pytest_cache",
        "-C", $root, ".")
    Write-Host ("tarball {0:N0} KB" -f ((Get-Item $tarball).Length / 1KB))
    Invoke-Native "scp" ($scp + @($tarball, "${SshHost}:$remoteTar"))
    Invoke-Native "scp" ($scp + @((Join-Path $root "scripts\deploy_remote.sh"), "${SshHost}:$remoteScript"))
    $remote = "bash $remoteScript $RemoteDir $remoteTar $stamp" + $(if ($Test) { " --test" } else { "" }) + "; rc=`$?; rm -f $remoteScript; exit `$rc"
    Invoke-Native "ssh" ($ssh + @($SshHost, $remote))
} finally {
    Remove-Item $tarball -ErrorAction SilentlyContinue
}
Write-Host "deployed $stamp"
