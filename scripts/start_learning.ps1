<# Upload a separate training workspace and start a bounded native-emulator RSI run.
The live pvz WebSocket service is not restarted. Secrets remain on the server.
#>
param(
    [ValidateRange(1,20)][int]$Rounds = 3,
    [string]$SshHost = "root@117.186.102.78",
    [int]$Port = 32420,
    [string]$Key = (Join-Path $PSScriptRoot "..\..\..\id_ed25519_llm2")
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Key = (Resolve-Path $Key).Path
$ssh = @("-T", "-i", $Key, "-p", $Port, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ConnectionAttempts=3")

function Run-Native([string]$exe, [string[]]$argv) {
    & $exe @argv
    if ($LASTEXITCODE -ne 0) { throw "$exe failed with exit code $LASTEXITCODE" }
}

$outputDir = Join-Path $root "data"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$archive = Join-Path $outputDir "learning-upload.tgz"
Run-Native "$env:SystemRoot\System32\tar.exe" @(
    "-czf", $archive, "--exclude", "__pycache__", "--exclude", ".pytest_cache", "-C", $root,
    "rsi", "rules", "server", "common", "client", "sim", "playbook_seed", "scripts", "tests", "knowledge", "pyproject.toml", "uv.lock"
)
# PowerShell 5 text pipes corrupt binary archives. Send only base64 ASCII over
# one SSH stdin stream, and decode it on the server before extracting.
$encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes($archive), [Base64FormattingOptions]::InsertLineBreaks)
$script = @'
set -euo pipefail
test -f /root/fkchang/pvz-agent/.env
if tmux has-session -t pvz-learning 2>/dev/null; then
  echo 'Learning is already running; inspect its log instead of starting a duplicate.'
  exit 0
fi
mkdir -p /root/fkchang/pvz-learning
base64 -d > /root/fkchang/pvz-learning-upload.tgz <<'PVZ_LEARNING_ARCHIVE'
ARCHIVE_PAYLOAD
PVZ_LEARNING_ARCHIVE
tar -xzf /root/fkchang/pvz-learning-upload.tgz -C /root/fkchang/pvz-learning
bash /root/fkchang/pvz-learning/scripts/start_learning_remote.sh ROUNDS_VALUE
exit 0
# End of script; any extra CRLF from the PowerShell text pipeline is unreachable.
'@
$script = $script.Replace("ARCHIVE_PAYLOAD", $encoded).Replace("ROUNDS_VALUE", [string]$Rounds).Replace("`r", "")
$script | & ssh @ssh $SshHost "bash -s"
if ($LASTEXITCODE -ne 0) { throw "Learning upload/start failed with exit code $LASTEXITCODE" }
