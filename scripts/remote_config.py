"""Shared configuration for Python deployment entry points."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.local_config import ssh_settings, remote_command  # noqa: E402,F401
