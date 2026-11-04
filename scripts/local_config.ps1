# Shared private SSH configuration. Dot-sourcing this file never connects.
$script:PvzConfigRoot = Split-Path -Parent $PSScriptRoot
function Get-PvzSshSettings {
    param([string]$SshHost='', [int]$Port=0, [string]$Key='', [string]$RemoteDir='', [switch]$RequireRemoteDir)
    $taskConfigPath = if ($env:PVZ_LOCAL_CONFIG) { $env:PVZ_LOCAL_CONFIG } else { Join-Path $script:PvzConfigRoot 'local.json' }
    $taskConfig = if (Test-Path -LiteralPath $taskConfigPath) { (Get-Content -LiteralPath $taskConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json).ssh } else { $null }
    if (-not $SshHost -and $taskConfig) { $SshHost=[string]$taskConfig.host }
    if (-not $Port) { $Port=if ($taskConfig -and $taskConfig.port) { [int]$taskConfig.port } else { 22 } }
    if (-not $Key -and $taskConfig) { $Key=[string]$taskConfig.key }
    if (-not $RemoteDir -and $taskConfig) { $RemoteDir=[string]$taskConfig.remote_dir }
    if (-not $SshHost -or -not $Key -or ($RequireRemoteDir -and -not $RemoteDir)) { throw 'Missing SSH settings. Copy local.example.json to local.json and configure it.' }
    if ($SshHost -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$' -or $Port -lt 1 -or $Port -gt 65535) { throw 'Invalid SSH host or port.' }
    if ($RemoteDir -and ($RemoteDir -notmatch '^/[A-Za-z0-9_./-]+$' -or $RemoteDir -eq '/' -or $RemoteDir -match '(^|/)\.\.(/|$)')) { throw 'remote_dir must be an absolute POSIX project path without spaces or shell syntax.' }
    if ($Key.StartsWith('~/') -or $Key.StartsWith('~\')) { $Key=Join-Path ([Environment]::GetFolderPath('UserProfile')) $Key.Substring(2) }
    $Key=[Environment]::ExpandEnvironmentVariables($Key)
    if (-not [IO.Path]::IsPathRooted($Key)) { $Key=Join-Path (Split-Path -Parent ([IO.Path]::GetFullPath($taskConfigPath))) $Key }
    return [pscustomobject]@{host=$SshHost;port=$Port;key=[IO.Path]::GetFullPath($Key);remote_dir=$RemoteDir}
}
