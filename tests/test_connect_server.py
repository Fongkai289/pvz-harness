"""Exercise the real PowerShell wrapper offline; no server or private key is used."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"),
                                reason="Windows PowerShell connection wrapper")
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "connect_server.ps1"

FAKE = r'''
$debugPath = $args[[Array]::IndexOf($args, '-E') + 1]
$count = 0
if (Test-Path -LiteralPath $env:PVZ_TEST_COUNT) { $count = [int][IO.File]::ReadAllText($env:PVZ_TEST_COUNT) }
$count++
[IO.File]::WriteAllText($env:PVZ_TEST_COUNT, [string]$count)
[IO.File]::WriteAllText($env:PVZ_TEST_ARGS, (ConvertTo-Json -InputObject @($args) -Compress))
$scenario = $env:PVZ_TEST_SCENARIO
$code = 255
$debug = 'Connection timed out'
switch ($scenario) {
    'recover' { if ($count -ge 3) { $code = 0; $debug = 'Authenticated to server using publickey.' } }
    'auth' { $debug = 'Permission denied (publickey).' }
    'hostkey' { $debug = 'Host key verification failed.' }
    'keyfile' { $debug = 'WARNING: UNPROTECTED PRIVATE KEY FILE!' }
    'forward' { $debug = 'Authenticated to server using publickey. bind: Address already in use' }
    'unknown' { $debug = '' }
    'command-error' { $code = 7; $debug = "Authenticated to server using publickey.`nSending command: PRIVATE_TEST_SENTINEL" }
    'lost-command' { $debug = "Authenticated to server using publickey.`nSending command: PRIVATE_TEST_SENTINEL`nConnection reset by peer" }
    'tunnel' { $debug = 'Authenticated to server using publickey. Connection reset by peer'; if ($count -ge 2) { $code = 0 } }
    'interrupt' { $code = 130; $debug = 'Connection timed out' }
    'success' { $code = 0; $debug = 'Authenticated to server using publickey.' }
}
[IO.File]::WriteAllText($debugPath, $debug)
if ($code -eq 0) { Write-Output 'PVZ_SSH_OK' }
exit $code
'''


@pytest.fixture
def connection(tmp_path):
    root = tmp_path / "path with spaces"
    root.mkdir()
    fake, key = root / "fake_ssh.ps1", root / "test_key"
    fake.write_text(FAKE, encoding="ascii")
    key.write_text("not a real key", encoding="ascii")
    count, arguments, logs = root / "count", root / "arguments.json", root / "logs"

    def run(scenario, mode="Probe", extra=()):
        env = {**os.environ, "PVZ_TEST_COUNT": str(count), "PVZ_TEST_ARGS": str(arguments),
               "PVZ_TEST_SCENARIO": scenario}
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            "-Mode", mode, "-SshPath", str(fake), "-Key", str(key), "-LogDirectory", str(logs),
            "-SshHost", "tester@server.example.com", "-Port", "22",
            "-MaxAttempts", "3", "-RetryBaseSeconds", "0", "-RetryMaxSeconds", "0", "-NoJitter",
            "-UseDefaultRoute",
            *extra,
        ], capture_output=True, text=True, timeout=30, env=env)
        events = [json.loads(line) for log in logs.glob("*.jsonl") for line in log.read_text(encoding="utf-8").splitlines()]
        return result, events, int(count.read_text()) if count.exists() else 0, arguments
    return run


def test_network_failures_retry_then_recover(connection):
    result, events, count, arguments = connection("recover")
    assert result.returncode == 0, result.stdout + result.stderr
    assert count == 3 and "PVZ_SSH_OK" in result.stdout
    assert [e["reason"] for e in events if e["event"] == "result"] == [
        "network-before-authentication", "network-before-authentication", "success"]
    args = json.loads(arguments.read_text())
    assert "StrictHostKeyChecking=yes" in args and "IdentitiesOnly=yes" in args
    assert "ServerAliveInterval=15" in args and "ServerAliveCountMax=3" in args
    assert args[args.index("-i") + 1].endswith("path with spaces\\test_key")
    assert not Path(args[args.index("-E") + 1]).exists()  # ephemeral verbose log cleaned up


def test_unreachable_port_stops_at_limit(connection):
    result, events, count, _ = connection("network")
    assert result.returncode == 255 and count == 3
    assert "Retry limit reached" in result.stdout


@pytest.mark.parametrize("scenario,reason", [
    ("auth", "authentication-failed"), ("hostkey", "host-key-verification"),
    ("keyfile", "identity-file-error"), ("forward", "forwarding-failed"),
    ("unknown", "unknown-failure"), ("interrupt", "interrupted"),
])
def test_nontransient_or_unknown_failure_is_not_retried(connection, scenario, reason):
    result, events, count, _ = connection(scenario)
    assert count == 1 and result.returncode != 0
    assert [e["reason"] for e in events if e["event"] == "result"] == [reason]


@pytest.mark.parametrize("scenario,exit_code", [("lost-command", 255), ("command-error", 7)])
def test_authenticated_command_is_never_replayed_or_logged(connection, scenario, exit_code):
    result, events, count, arguments = connection(scenario, "Command", ("-RemoteCommand", "echo PRIVATE_TEST_SENTINEL"))
    assert result.returncode == exit_code and count == 1
    assert "Not replaying" in result.stdout
    assert "PRIVATE_TEST_SENTINEL" not in json.dumps(events) + result.stdout + result.stderr
    assert json.loads(arguments.read_text())[-1] == "echo PRIVATE_TEST_SENTINEL"


def test_only_tunnel_reconnects_after_authentication(connection):
    result, events, count, arguments = connection("tunnel", "Tunnel")
    assert result.returncode == 0 and count == 2
    args = json.loads(arguments.read_text())
    assert "-N" in args and "ExitOnForwardFailure=yes" in args
    assert args[args.index("-L") + 1] == "127.0.0.1:8765:127.0.0.1:8765"
    assert any(e.get("reason") == "tunnel-disconnected" for e in events)


def test_shell_allocates_tty_and_keeps_stdin(connection):
    result, events, count, arguments = connection("success", "Shell")
    assert result.returncode == 0
    args = json.loads(arguments.read_text())
    assert "-tt" in args and "-n" not in args


def test_remote_command_quotes_and_backslashes_survive_windows_argument_passing(connection):
    command = 'printf "%s\\n" "a b"; echo "$HOME"; echo \'literal\\path\''
    result, events, count, arguments = connection("success", "Command", ("-RemoteCommand", command))
    assert result.returncode == 0, result.stderr
    assert json.loads(arguments.read_text())[-1] == command


def test_bad_arguments_fail_before_starting_ssh(connection):
    result, events, count, _ = connection("success", "Command")
    assert result.returncode == 1 and count == 0
    assert "requires -RemoteCommand" in result.stderr


def test_backoff_is_exponential_and_capped():
    path = str(SCRIPT).replace("'", "''")
    command = f". '{path}'; @(1..6 | ForEach-Object {{ Get-SshRetryDelay $_ 2 20 $false }}) | ConvertTo-Json -Compress"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [2000, 4000, 8000, 16000, 20000, 20000]


@pytest.mark.parametrize("case,expected", [
    ("wifi", "192.0.2.9"), ("renamed", "192.0.2.9"),
    ("disconnected", ""), ("no_gateway", ""), ("default", ""),
    ("explicit", "192.0.2.4"), ("missing", "ERROR"),
])
def test_network_selection(case, expected):
    path = str(SCRIPT).replace("'", "''")
    command = f". '{path}'; $case = '{case}'; " + r'''
function Get-NetAdapter {
    [pscustomobject]@{Name='Phone'; Status='Up'; NdisPhysicalMedium=0; ifIndex=4}
    [pscustomobject]@{Name=$(if ($case -eq 'renamed') {'Custom adapter'} else {'WLAN'});
        Status=$(if ($case -eq 'disconnected') {'Disconnected'} else {'Up'});
        NdisPhysicalMedium=9; ifIndex=9}
}
function Get-NetRoute {
    param($InterfaceIndex, $AddressFamily, $ErrorAction)
    if ($case -ne 'no_gateway') { [pscustomobject]@{DestinationPrefix='0.0.0.0/0'} }
}
function Get-NetIPAddress {
    param($InterfaceIndex, $AddressFamily, $ErrorAction)
    [pscustomobject]@{IPAddress="192.0.2.$InterfaceIndex"; AddressState='Preferred'; SkipAsSource=$false}
}
try {
    $requested = if ($case -eq 'explicit') {'Phone'} elseif ($case -eq 'missing') {'Absent'} else {''}
    $value = Get-SshBindAddress -RequestedInterface $requested -DefaultRoute:($case -eq 'default')
    ConvertTo-Json -InputObject ([string]$value) -Compress
} catch { ConvertTo-Json -InputObject 'ERROR' }
'''
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
