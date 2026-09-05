"""Verify or refresh the bundled GW1000 parser from a fixed upstream repository."""

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/weewx_php_ingest/_vendor"
REPOSITORY = "https://github.com/weewx-contrib/weewx-gw1000"


def download(url, limit):
    host = urlsplit(url).hostname
    if urlsplit(url).scheme != "https" or host not in (
        "api.github.com",
        "raw.githubusercontent.com",
    ):
        raise ValueError("unexpected source")
    with urllib.request.urlopen(url, timeout=30) as response:
        if urlsplit(response.url).hostname != host or urlsplit(response.url).scheme != "https":
            raise ValueError("unexpected redirect")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("source exceeds limit")
    return data


def check():
    manifest = json.loads((TARGET / "gw1000-source.json").read_text())
    if (
        manifest["repository"] != REPOSITORY
        or not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"])
        or hashlib.sha256((TARGET / "gw1000.py").read_bytes()).hexdigest() != manifest["sha256"]
        or not (TARGET / "LICENSE-gw1000").is_file()
    ):
        raise ValueError("GW1000 source differs from manifest")


def update():
    info = json.loads(
        download("https://api.github.com/repos/weewx-contrib/weewx-gw1000/commits/master", 1000000)
    )
    commit = info["sha"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid revision")
    base = "https://raw.githubusercontent.com/weewx-contrib/weewx-gw1000/" + commit
    source = download(base + "/bin/user/gw1000.py", 2000000)
    license_text = download(base + "/LICENSE", 100000)
    compile(source, "gw1000.py", "exec")
    manifest = {
        "repository": REPOSITORY,
        "commit": commit,
        "sha256": hashlib.sha256(source).hexdigest(),
    }
    (TARGET / "gw1000.py").write_bytes(source)
    (TARGET / "LICENSE-gw1000").write_bytes(license_text)
    (TARGET / "gw1000-source.json").write_text(json.dumps(manifest, indent=2) + "\n")
    check()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    update() if args.update else check()
