param([switch]$DownloadToolchain)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$arguments = @((Join-Path $PSScriptRoot 'install_emulator.py'))
if ($DownloadToolchain) { $arguments += '--download-toolchain' }
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Emulator installation failed' }
