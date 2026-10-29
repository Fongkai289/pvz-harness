#!/usr/bin/env sh
# Requires Python development headers and a C++17 g++; run in the intended venv.
set -eu
exec python "$(dirname "$0")/install_emulator.py" "$@"
