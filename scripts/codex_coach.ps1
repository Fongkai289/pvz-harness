[CmdletBinding()]
param([ValidateSet('Start','Pause','Resume','Stop','Status','Open')][string]$Mode='Status')
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$taskState=Join-Path $taskRoot 'data\codex_coach_worker'
$taskPython=Join-Path $taskRoot '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Path $taskState -Force | Out-Null
if ($Mode -eq 'Stop') { New-Item -ItemType File -Path (Join-Path $taskState 'STOP') -Force | Out-Null; return }
if ($Mode -eq 'Pause') { New-Item -ItemType File -Path (Join-Path $taskState 'PAUSE') -Force | Out-Null; return }
if ($Mode -eq 'Open') { Start-Process (Join-Path $taskState 'tokens.html'); return }
if ($Mode -eq 'Status') {
    $taskUsage=Join-Path $taskState 'tokens.json'
    if (Test-Path -LiteralPath $taskUsage) { Get-Content -LiteralPath $taskUsage -Encoding UTF8 }
    else { Write-Output 'No Coach usage recorded yet.' }
    return
}
Remove-Item -LiteralPath (Join-Path $taskState 'PAUSE') -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $taskState 'STOP') -ErrorAction SilentlyContinue
$taskPidFile=Join-Path $taskState 'worker.pid'
if (Test-Path -LiteralPath $taskPidFile) {
    $taskOldPid=[int](Get-Content -LiteralPath $taskPidFile)
    $taskOld=Get-CimInstance Win32_Process -Filter "ProcessId=$taskOldPid" -ErrorAction SilentlyContinue
    if ($taskOld -and $taskOld.CommandLine -like '*rsi.codex_coach*') { Write-Output "Coach already running: $taskOldPid"; return }
}
$taskWorker=Start-Process -FilePath $taskPython -ArgumentList @('-X','utf8','-u','-m','rsi.codex_coach') -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskState 'worker.log') -RedirectStandardError (Join-Path $taskState 'worker-error.log')
Set-Content -LiteralPath $taskPidFile -Value $taskWorker.Id
Write-Output "Coach started: $($taskWorker.Id). Token report: $taskState\tokens.html"
