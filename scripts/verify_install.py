"""Verify installed WeeWX drivers match the bundled source byte for byte."""

import hashlib
import json
import sys
from pathlib import Path

import weewx

source = Path(sys.argv[1])
manifest = json.loads((source / "vendor/weewx-source.json").read_text())
if weewx.__version__ != manifest["version"]:
    raise SystemExit("Installed WeeWX version differs from bundled source")
installed = Path(weewx.__file__).parent
count = 0
for name, expected in manifest["files"].items():
    if name.startswith("src/weewx/drivers/") and name.endswith(".py"):
        path = installed / name.removeprefix("src/weewx/")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit("Installed driver differs from bundled source: " + path.name)
        count += 1
print(f"WeeWX {weewx.__version__}: {count} driver files verified")
