<#
Windows PowerShell 5.1+ SSH wrapper. No Python or additional packages needed.
Retries transient failures before authentication. Commands/shells are NEVER
automatically replayed after authentication: their remote outcome may be unknown.
Tunnel mode may reconnect because it runs no remote application command.
#>
[CmdletBinding()]
param(
    [ValidateSet('Shell', 'Probe', 'Command', 'Tunnel')][string]$Mode = 'Shell',
    [string]$SshHost = 'operator@server.example.com',
    [ValidateRange(1,65535)][int]$Port = 22,
    [string]$Key = '',
    [string]$InterfaceAlias = '',
    [switch]$UseDefaultRoute,
    [Alias('Command')][string]$RemoteCommand = '',
    [ValidateRange(1,30)][int]$MaxAttempts = 6,
    [ValidateRange(1,120)][int]$ConnectTimeoutSeconds = 15,
    [ValidateRange(1,120)][int]$AliveIntervalSeconds = 15,
    [ValidateRange(1,10)][int]$AliveCount = 3,
    [ValidateRange(0,120)][int]$RetryBaseSeconds = 2,
    [ValidateRange(0,120)][int]$RetryMaxSeconds = 20,
    [switch]$NoJitter,
    [ValidateRange(1,65535)][int]$LocalPort = 8765,
    [ValidateRange(1,65535)][int]$RemotePort = 8765,
    [string]$LogDirectory = '',
    [string]$SshPath = ''
)

# Resolve defaults after parameter binding; PSScriptRoot may be empty while
# Windows PowerShell evaluates parameter default expressions.
$script:ConnectionScriptRoot = $PSScriptRoot
if (-not $script:ConnectionScriptRoot) {
    $script:ConnectionScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Get-SshOutcome {
    param([int]$ExitCode, [string]$DebugText, [string]$ConnectionMode)
    $authenticated = $DebugText -match 'Authenticated to |Authentication succeeded|Sending command:|Entering interactive session'
    $reason = 'unknown-failure'
    $retry = $false
    if ($ExitCode -eq 0) {
        $reason = 'success'
    } elseif ($ExitCode -in @(130, -1073741510)) {
        $reason = 'interrupted'
    } elseif ($DebugText -match 'REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed|No .* host key is known') {
        $reason = 'host-key-verification'
    } elseif ($DebugText -match 'UNPROTECTED PRIVATE KEY FILE|bad permissions|Load key .*|no such identity|Identity file .*not accessible') {
        $reason = 'identity-file-error'
    } elseif ($DebugText -match 'Permission denied \(|No more authentication methods') {
        $reason = 'authentication-failed'
    } elseif ($DebugText -match 'Address already in use|cannot listen to port|Could not request local forwarding|administratively prohibited') {
        $reason = 'forwarding-failed'
    } elseif ($authenticated) {
        if ($ConnectionMode -eq 'Tunnel' -and $ExitCode -eq 255) {
            $reason = 'tunnel-disconnected'
            $retry = $true
        } else {
            $reason = 'session-ended-after-authentication'
        }
    } elseif ($ExitCode -eq 255 -and $DebugText -match 'Connection (timed out|refused|reset|closed)|No route to host|Network is unreachable|Could not resolve hostname|Connection timed out during banner exchange|kex_exchange_identification:') {
        $reason = 'network-before-authentication'
        $retry = $true
    } elseif ($ExitCode -ne 255) {
        $reason = 'command-or-client-exit'
    }
    return [pscustomobject]@{ Reason=$reason; Retry=$retry; Authenticated=$authenticated }
}

function Get-SshBindAddress {
    param([string]$RequestedInterface, [switch]$DefaultRoute)
    if ($RequestedInterface -and $DefaultRoute) {
        throw 'InterfaceAlias and UseDefaultRoute cannot be combined.'
    }
    if ($DefaultRoute) { return '' }
    try {
        $adapters = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
            $_.Status -eq 'Up' -and (
                ($RequestedInterface -and $_.Name -eq $RequestedInterface) -or
                (-not $RequestedInterface -and $_.NdisPhysicalMedium -in @(1, 9))
            )
        } | Sort-Object ifIndex)
        foreach ($adapter in $adapters) {
            $gateway = @(Get-NetRoute -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' })
            if (-not $gateway) { continue }
            $address = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction Stop |
                Where-Object { $_.AddressState -eq 'Preferred' -and -not $_.SkipAsSource -and
                    $_.IPAddress -notlike '169.254.*' } | Sort-Object IPAddress | Select-Object -First 1
            if ($address) { return [string]$address.IPAddress }
        }
    } catch {
        if ($RequestedInterface) { throw }
        Write-Warning 'Could not inspect Wi-Fi adapters; using the system default route.'
    }
    if ($RequestedInterface) { throw "Interface '$RequestedInterface' has no usable IPv4 address and default route." }
    return ''
}

function Get-SshRetryDelay {
    param([int]$Attempt, [int]$Base, [int]$Cap, [bool]$Jitter)
    $delay = [Math]::Min($Cap, $Base * [Math]::Pow(2, $Attempt - 1))
    if ($Jitter -and $delay -gt 0) {
        $delay = [Math]::Min($Cap, $delay * (1 + (Get-Random -Minimum 0 -Maximum 251) / 1000.0))
    }
    return [int][Math]::Round(1000 * $delay)
}

function Write-SshEvent {
    param([string]$Path, [hashtable]$Event)
    $Event['time'] = [DateTimeOffset]::Now.ToString('o')
    $line = ($Event | ConvertTo-Json -Compress -Depth 4) + [Environment]::NewLine
    [IO.File]::AppendAllText($Path, $line, [Text.UTF8Encoding]::new($false))
}

function Read-SharedText {
    param([string]$Path)
    # ssh keeps its -E log open for writing; File.ReadAllText refuses to share with a writer.
    try {
        $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    } catch [IO.IOException] { return '' }
    try { return [IO.StreamReader]::new($stream).ReadToEnd() } finally { $stream.Dispose() }
}

function ConvertTo-NativeCommandLine {
    param([string[]]$Arguments)
    # Windows C argv quoting. PowerShell 5's native invocation otherwise drops
    # embedded quotes in commands such as tmux ... "command > logfile".
    $quoted = foreach ($item in $Arguments) {
        $escaped = [regex]::Replace($item, '(\\*)"', '$1$1\"')
        $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
        '"' + $escaped + '"'
    }
    return $quoted -join ' '
}

function Invoke-StableSsh {
    $script:ConnectionExitCode = 1
    if (-not $Key) { $Key = Join-Path $script:ConnectionScriptRoot '..\..\..\pvz_operator' }
    if (-not $LogDirectory) { $LogDirectory = Join-Path $script:ConnectionScriptRoot '..\logs\ssh' }
    if ($SshHost -notmatch '^[A-Za-z0-9_\[][A-Za-z0-9_.@:\[\]-]*$') {
        throw 'SshHost must be a single SSH destination, such as operator@server.example.com.'
    }
    if ($RemoteCommand) {
        if ($Mode -eq 'Shell') { $Mode = 'Command' }
        if ($Mode -ne 'Command') { throw 'RemoteCommand is only valid in Command mode.' }
    } elseif ($Mode -eq 'Command') {
        throw 'Command mode requires -RemoteCommand.'
    }
    if ($RetryBaseSeconds -gt $RetryMaxSeconds) { throw 'RetryBaseSeconds must not exceed RetryMaxSeconds.' }
    $identity = (Resolve-Path -LiteralPath $Key -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $identity -PathType Leaf)) { throw 'Key must be an existing file.' }
    if (-not $SshPath) { $SshPath = (Get-Command ssh.exe -ErrorAction Stop).Source }
    $sshExecutable = (Resolve-Path -LiteralPath $SshPath -ErrorAction Stop).Path
    [IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($LogDirectory)) | Out-Null
    $logName = 'connect-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N') + '.jsonl'
    $eventLog = Join-Path ([IO.Path]::GetFullPath($LogDirectory)) $logName
    Write-Host "SSH target: ${SshHost}:$Port ($Mode). Diagnostic log: $eventLog"
    if ($Mode -eq 'Tunnel') {
        Write-Host "Forward: 127.0.0.1:$LocalPort -> server 127.0.0.1:$RemotePort. Ctrl+C stops the tunnel."
    }

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        # Resolve again on reconnect: DHCP addresses and active adapters can change.
        $bindAddress = Get-SshBindAddress -RequestedInterface $InterfaceAlias -DefaultRoute:$UseDefaultRoute
        if ($bindAddress) { Write-Host "Network: bind to $bindAddress (preferred Wi-Fi or requested interface)." }
        else { Write-Host 'Network: system default route (no Wi-Fi selected).' }
        $debugLog = [IO.Path]::GetTempFileName()
        $started = [Diagnostics.Stopwatch]::StartNew()
        $sshArguments = @(
            '-v', '-E', $debugLog, '-i', $identity, '-p', [string]$Port,
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', "ConnectTimeout=$ConnectTimeoutSeconds", '-o', 'ConnectionAttempts=1',
            '-o', "ServerAliveInterval=$AliveIntervalSeconds", '-o', "ServerAliveCountMax=$AliveCount",
            '-o', 'TCPKeepAlive=yes', '-o', 'ExitOnForwardFailure=yes'
        )
        if ($bindAddress) { $sshArguments += @('-b', $bindAddress) }
        if ($Mode -eq 'Shell') {
            $sshArguments += '-tt'
        } else {
            $sshArguments += @('-T', '-n')
        }
        if ($Mode -eq 'Tunnel') {
            $sshArguments += @('-N', '-L', "127.0.0.1:${LocalPort}:127.0.0.1:$RemotePort")
        }
        $sshArguments += $SshHost
        if ($Mode -eq 'Probe') { $sshArguments += 'echo PVZ_SSH_OK' }
        if ($Mode -eq 'Command') { $sshArguments += $RemoteCommand }

        Write-Host "Attempt $attempt/$MaxAttempts ..."
        Write-SshEvent $eventLog @{event='attempt'; attempt=$attempt; target=$SshHost; port=$Port; mode=$Mode; bind_address=$bindAddress}
        $process = $null
        try {
            # Launch with an explicit argument string so Windows PowerShell 5
            # preserves quotes/backslashes. Inherit the caller's terminal streams.
            $start = [Diagnostics.ProcessStartInfo]::new()
            $start.UseShellExecute = $false
            $start.FileName = $sshExecutable
            $launchArguments = $sshArguments
            if ([IO.Path]::GetExtension($sshExecutable) -eq '.ps1') {
                $start.FileName = (Get-Command powershell.exe -ErrorAction Stop).Source
                $launchArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $sshExecutable) + $sshArguments
            }
            $start.Arguments = ConvertTo-NativeCommandLine $launchArguments
            $process = [Diagnostics.Process]::Start($start)
            $announced = $false
            while (-not $process.WaitForExit(250)) {
                if ($Mode -eq 'Tunnel' -and -not $announced) {
                    $progress = Read-SharedText $debugLog
                    if ($progress -match 'Entering interactive session' -and $progress -match 'Local forwarding listening') {
                        Write-Host 'Tunnel connected. Keep this window open; Ctrl+C stops it.'
                        $announced = $true
                    }
                }
            }
            $connectionExit = $process.ExitCode
            $debugText = Read-SharedText $debugLog
            $outcome = Get-SshOutcome $connectionExit $debugText $Mode
        } finally {
            if ($null -ne $process) {
                if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
                $process.Dispose()
            }
            $started.Stop()
            # Debug lines may include the remote command; do not retain them.
            Remove-Item -LiteralPath $debugLog -Force -ErrorAction SilentlyContinue
        }
        Write-SshEvent $eventLog @{event='result'; attempt=$attempt; reason=$outcome.Reason;
            authenticated=$outcome.Authenticated; exit_code=$connectionExit; elapsed_seconds=[Math]::Round($started.Elapsed.TotalSeconds, 2)}
        if ($connectionExit -eq 0) {
            Write-Host 'SSH completed successfully.'
            $script:ConnectionExitCode = 0
            return
        }
        Write-Host "SSH exit ${connectionExit}: $($outcome.Reason)"
        if (-not $outcome.Retry -or $attempt -eq $MaxAttempts) {
            if ($outcome.Authenticated -and $Mode -eq 'Command') {
                Write-Host 'Not replaying the command. It may already have run; inspect remote state before retrying.'
            } elseif ($outcome.Reason -eq 'host-key-verification') {
                Write-Host 'Verify the server host key manually. This script does not disable verification or edit known_hosts.'
            } elseif ($outcome.Reason -eq 'network-before-authentication') {
                Write-Host 'Retry limit reached before authentication. Check the server SSH address/port mapping and network.'
            } elseif ($outcome.Reason -eq 'forwarding-failed') {
                Write-Host 'Check the local port or server forwarding policy. No existing process will be stopped.'
            }
            $script:ConnectionExitCode = $connectionExit
            return
        }
        $delayMs = Get-SshRetryDelay $attempt $RetryBaseSeconds $RetryMaxSeconds (-not $NoJitter)
        Write-Host "Retrying in $([Math]::Round($delayMs / 1000.0, 2)) seconds ..."
        Write-SshEvent $eventLog @{event='retry'; attempt=$attempt; delay_ms=$delayMs}
        if ($delayMs -gt 0) { Start-Sleep -Milliseconds $delayMs }
    }
}

# Dot-sourcing loads the diagnostic functions for offline tests without connecting.
if ($MyInvocation.InvocationName -ne '.') {
    try {
        Invoke-StableSsh
        exit $script:ConnectionExitCode
    } catch {
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    }
}
