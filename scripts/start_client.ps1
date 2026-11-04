<#
Starts the real-game client: the SSH tunnel to the server (kept alive by connect_server.ps1 -Mode Tunnel)
and client.windows_client. PvZ may be opened before or after; the client attaches to it. Its menus stay manual.

The token is read from the server's .env over SSH. If that fails it falls back to $env:PVZ_TOKEN or your
user environment variable PVZ_TOKEN, and only then asks in this window. It lives only in this process's
memory, never in a file.

    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_client.ps1
#>
[CmdletBinding()]
param([ValidateRange(5,120)][int]$TunnelWaitSeconds = 40)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$Host.UI.RawUI.WindowTitle = 'PvZ 客户端启动'

$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*-m client.windows_client*' }
if ($running) {
    Write-Host "客户端已经在运行（进程 $($running.ProcessId -join ', ')），请切到“PvZ 控制中心”窗口。"
    Start-Sleep -Seconds 5
    return
}

$listening = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Host '正在打开到服务器的 SSH 隧道（单独的最小化窗口，断线会自动重连）…'
    $tunnel = Join-Path $PSScriptRoot 'connect_server.ps1'
    Start-Process powershell.exe -WindowStyle Minimized -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$tunnel`"", '-Mode', 'Tunnel')
    $deadline = (Get-Date).AddSeconds($TunnelWaitSeconds)
    while (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host "隧道 $TunnelWaitSeconds 秒内没有建立，请看最小化的隧道窗口里的错误。"
            Read-Host '按回车关闭'
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host '隧道已连接。'
}

function Get-ServerToken {
    # Reads PVZ_TOKEN from the server's .env over the same SSH key as the tunnel; the value stays in memory.
    $ErrorActionPreference = 'Continue'
    try {
        . (Join-Path $PSScriptRoot 'connect_server.ps1')  # dot-sourced: loads helpers, does not connect
        $bind = Get-SshBindAddress
        $taskSsh=Get-PvzSshSettings -RequireRemoteDir
        $key = (Resolve-Path -LiteralPath $taskSsh.key).Path
        $sshArgs = @('-i', $key, '-p', [string]$taskSsh.port, '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15', '-o', 'LogLevel=ERROR', '-T', '-n')
        if ($bind) { $sshArgs += @('-b', $bind) }
        $line = & ssh.exe @sshArgs $taskSsh.host `
            "sed -n 's/^PVZ_TOKEN=//p' '$($taskSsh.remote_dir)/.env' | tail -n 1" 2>$null
        if ($LASTEXITCODE -ne 0) { return '' }
        return ([string]$line).Trim().Trim('"').Trim("'")
    } catch { return '' }
}

# The server's .env is authoritative, so it wins over a stale or mistyped user variable.
Write-Host '正在从服务器读取令牌…'
$serverToken = Get-ServerToken
if ($serverToken) {
    $env:PVZ_TOKEN = $serverToken
    Write-Host '令牌已读取（只在内存里）。'
}
if (-not $env:PVZ_TOKEN) { $env:PVZ_TOKEN = [Environment]::GetEnvironmentVariable('PVZ_TOKEN', 'User') }
if (-not $env:PVZ_TOKEN) {
    $secure = Read-Host '请输入 PVZ_TOKEN（服务器项目 .env 里的值；只保存在这个窗口的内存里）' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:PVZ_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if (-not $env:PVZ_TOKEN) { Write-Host '没有输入令牌。'; Read-Host '按回车关闭'; exit 1 }
}

$Host.UI.RawUI.WindowTitle = 'PvZ 客户端日志'
Write-Host '正在启动客户端，稍后会在游戏旁边弹出“PvZ 控制中心”窗口。进入关卡、选好卡后在那里点“开始”。这个窗口是客户端日志，关掉它客户端也会退出。'
Set-Location $root
& $python -X utf8 -u -m client.windows_client
if ($LASTEXITCODE) { Read-Host "客户端退出（代码 $LASTEXITCODE），按回车关闭" }
