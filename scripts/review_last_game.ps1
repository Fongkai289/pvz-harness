<# Review the newest closed real-game session on the configured server. No promotion. #>
[CmdletBinding()]
param([ValidatePattern('^[a-zA-Z0-9_-]+$')][string]$Session = '')
$selector = if ($Session) { "--session $Session" } else { '--latest' }
$command = "cd /srv/pvz-harness && .venv/bin/python -m rsi.review_live $selector"
& (Join-Path $PSScriptRoot 'connect_server.ps1') -Mode Command -RemoteCommand $command
exit $LASTEXITCODE
