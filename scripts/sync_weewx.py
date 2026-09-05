"""Vendor the official WeeWX source release and keep the runtime pin in lockstep."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"
TARGET = VENDOR / "weewx"
MANIFEST = VENDOR / "weewx-source.json"


def download(url, limit):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ("pypi.org", "files.pythonhosted.org"):
        raise ValueError("unexpected upstream URL")
    with urllib.request.urlopen(url, timeout=60) as response:
        final = urlsplit(response.url)
        if final.scheme != "https" or final.hostname != parsed.hostname:
            raise ValueError("unexpected upstream redirect")
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("upstream response too large")
    return body


def tree_hashes(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
        and p.relative_to(path).parts[0] not in ("build", "dist")
        and "__pycache__" not in p.parts
        and not p.name.endswith(".pyc")
        and not any(x.endswith(".egg-info") for x in p.parts)
    }


def check():
    manifest = json.loads(MANIFEST.read_text())
    if tree_hashes(TARGET) != manifest["files"]:
        raise ValueError("vendored WeeWX source differs from manifest")
    pin = f'"weewx=={manifest["version"]}"'
    if pin not in (ROOT / "pyproject.toml").read_text():
        raise ValueError("WeeWX dependency pin differs from vendored source")
    if not (TARGET / "src/weewx/drivers/vantage.py").is_file():
        raise ValueError("bundled drivers missing")
    return manifest


def extract(source, destination, version):
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        total = 0
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if (
                not parts
                or parts[0] != f"weewx-{version}"
                or ".." in parts
                or "\\" in member.name
                or any(":" in p for p in parts)
            ):
                raise ValueError("invalid upstream archive path")
            if len(parts) == 1:
                continue
            if not member.isdir() and not member.isfile():
                raise ValueError("upstream archive contains links or special files")
            path = destination.joinpath(*parts[1:])
            if not path.resolve().is_relative_to(destination.resolve()):
                raise ValueError("archive path escaped destination")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                total += member.size
                if total > 100_000_000:
                    raise ValueError("upstream archive too large")
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as data:
                    path.write_bytes(data.read())


def sync(version=None):
    metadata = json.loads(download("https://pypi.org/pypi/weewx/json", 4_000_000))
    version = version or metadata["info"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("only stable WeeWX releases are supported")
    if version != metadata["info"]["version"]:
        metadata = json.loads(download(f"https://pypi.org/pypi/weewx/{version}/json", 4_000_000))
    source = next(f for f in metadata["urls"] if f["packagetype"] == "sdist")
    digest = source["digests"]["sha256"]
    if MANIFEST.exists():
        current = check()
        if current["version"] == version and current["sha256"] == digest:
            print(f"WeeWX {version}: current")
            return False
    body = download(source["url"], 20_000_000)
    if hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("upstream archive checksum mismatch")
    VENDOR.mkdir(exist_ok=True)
    if TARGET.is_symlink() or TARGET.resolve() != ROOT.resolve() / "vendor" / "weewx":
        raise ValueError("unsafe vendor destination")
    with tempfile.TemporaryDirectory(prefix="weewx-sync-", dir=VENDOR) as work:
        staged = Path(work) / "source"
        staged.mkdir()
        extract(body, staged, version)
        if not (staged / "LICENSE.txt").is_file():
            raise ValueError("upstream license missing")
        files = tree_hashes(staged)
        old = Path(work) / "previous"
        if TARGET.exists():
            TARGET.rename(old)
        try:
            staged.rename(TARGET)
        except BaseException:
            if old.exists():
                old.rename(TARGET)
            raise
        # TemporaryDirectory only removes this verified subtree beneath vendor/.
        if old.exists():
            if not old.resolve().is_relative_to(VENDOR.resolve()):
                raise ValueError("unsafe cleanup path")
            shutil.rmtree(old)
    MANIFEST.write_text(
        json.dumps(
            {
                "project": "https://github.com/weewx/weewx",
                "version": version,
                "url": source["url"],
                "sha256": digest,
                "files": files,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    project = ROOT / "pyproject.toml"
    content, count = re.subn(r'"weewx==[0-9.]+"', f'"weewx=={version}"', project.read_text())
    if count != 1:
        raise ValueError("expected exactly one WeeWX dependency pin")
    project.write_text(content, encoding="utf-8")
    check()
    print(f"WeeWX {version}: synchronized")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--version")
    args = parser.parse_args()
    if args.check:
        print(f"WeeWX {check()['version']}: source verified")
    else:
        changed = sync(args.version)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
                out.write(f"changed={str(changed).lower()}\n")
