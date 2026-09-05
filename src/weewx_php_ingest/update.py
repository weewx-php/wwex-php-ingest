"""Delegate managed Linux updates to the installation's root-owned installer."""

import os
import subprocess
import sys
from pathlib import Path

from .config import ConfigError


def update():
    if os.name != "posix" or not hasattr(os, "geteuid"):
        raise ConfigError("system updates require Linux")
    if os.geteuid() != 0:
        raise ConfigError("run: sudo weewx-php-ingest update")
    installer = Path(sys.prefix).resolve().parent / "source" / "install.sh"
    if not installer.is_file() or installer.stat().st_uid != 0:
        raise ConfigError("no managed installation; run install.sh first")
    if installer.stat().st_mode & 0o022:
        raise ConfigError("installer must not be writable by group or others")
    return subprocess.call(
        ["bash", str(installer), "--update", "--prefix", str(installer.parents[3])]
    )
