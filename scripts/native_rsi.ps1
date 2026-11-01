[CmdletBinding()]
param(
    [ValidateSet('Check','Run','Watch','Pause','Resume','Stop')][string]$Mode = 'Check',
    [string]$Session = '',
    [switch]$Retry,
    [string]$Config = ''
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $Config) { $Config = Join-Path $taskRoot 'data\native_rsi\config.json' }
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Config)) { throw "Missing native RSI config: $Config" }
$arguments = @('-X','utf8','-u','-m','rsi.native_loop','--config',$Config)
if ($Mode -eq 'Check') { $arguments += '--preflight' }
elseif ($Mode -eq 'Watch') { $arguments += '--watch' }
elseif ($Mode -in @('Pause','Resume','Stop')) { $arguments += @('--control',$Mode.ToLowerInvariant()) }
if ($Session) { $arguments += @('--session',$Session) }
if ($Retry) { $arguments += '--retry' }
Push-Location $taskRoot
try { & $taskPython @arguments; $taskExit = $LASTEXITCODE } finally { Pop-Location }
exit $taskExit
