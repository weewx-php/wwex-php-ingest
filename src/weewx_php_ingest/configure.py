"""Guided station setup with locally generated credentials."""

import contextlib
import importlib
import io
import os
import re
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from configobj import ConfigObj

from .config import ConfigError, endpoint_url, load_config, read_token, read_weewx
from .hardware import drivers, probe_packet, scan, service_account
from .protocol import event_from_loop
from .runtime import driver_config
from .supervisor import open_spools
from .transport import HTTPSClient, TransportError
from .uploader import Uploader


def write_config(path, data, *, replace=False, validate=True):
    path = Path(path).resolve()
    previous = path.stat() if path.exists() else None
    if previous and not replace:
        raise ConfigError("configuration already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    config = ConfigObj(data, encoding="utf-8", interpolation=False)
    fd, temporary = tempfile.mkstemp(prefix=".weewx-", suffix=".conf", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            config.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if validate:
            loaded = load_config(temporary)
            read_token(loaded)
            for station in loaded.stations:
                driver_config(station)
        account = service_account()
        if previous and os.name != "nt":
            os.chown(temporary, previous.st_uid, previous.st_gid)
        elif account:
            os.chown(temporary, account.pw_uid, account.pw_gid)
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def initial_data(state_dir):
    return {
        "WEEWX_ROOT": state_dir,
        "USER_ROOT": "bin/user",
        "Station": {
            "station_type": "Simulator",
            "latitude": "0",
            "longitude": "0",
            "altitude": ["0", "meter"],
        },
        "Simulator": {"driver": "weewx.drivers.simulator", "loop_interval": "2.5"},
        "Ingest": {
            "collector_id": str(uuid.uuid4()),
            "url": "",
            "token": secrets.token_hex(32),
            "station_key": "station",
            "state_dir": state_dir,
            "send_interval": "10",
        },
    }


def initialize(path, state_dir):
    if not Path(path).exists():
        write_config(path, initial_data(state_dir), validate=False)


def ask(label, default=None, choices=None):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        # Also works while suppressing upstream conf-editor explanatory output.
        print(f"{label}{suffix}: ", end="", flush=True, file=sys.__stdout__)
        value = input().strip() or (str(default) if default is not None else "")
        if value and (not choices or value in choices):
            return value
        print("Invalid value")


def yes(label, default=False):
    return ask(label + " (y/n)", "y" if default else "n", ("y", "n")) == "y"


def domain_url(value):
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    value = value.rstrip("/")
    if not value.endswith("/ingest/weewx.php"):
        value += "/ingest/weewx.php"
    return endpoint_url(value)


def select_hardware(existing):
    devices, warnings = scan()
    print("Connected devices")
    for device in devices:
        print(f"  {device['path']}  {device['description']}  {device['driver'] or ''}")
    if not devices:
        print("  None detected")
    for warning in warnings:
        print(warning)
    choices = drivers()
    suggested = next((d["driver"] for d in devices if d["driver"]), None)
    old_type = existing.get("Station", {}).get("station_type")
    default = old_type or suggested or "Vantage"
    for index, (name, _module) in enumerate(choices, 1):
        print(f"  {index}. {name}")
    selected = ask("Hardware", default)
    while True:
        match = next(
            (
                (name, module)
                for i, (name, module) in enumerate(choices, 1)
                if selected.lower() == name.lower() or selected == str(i)
            ),
            None,
        )
        if match:
            break
        selected = ask("Hardware")
    name, module = match
    try:
        editor = importlib.import_module(module).confeditor_loader()
        raw = ConfigObj(editor.default_stanza.splitlines(), interpolation=False).dict()
        options = {**raw[name], **existing.get(name, {})}
        ports = [d["path"] for d in devices if d["kind"] == "serial"]
        if ports and "port" in options and "port" not in existing.get(name, {}):
            options["port"] = ports[0]

        def prompt(label, dflt=None, opts=None):
            return ask(label, options.get(label, dflt), opts)

        editor._prompt = prompt
        editor.existing_options = options
        with contextlib.redirect_stdout(io.StringIO()):
            options.update(editor.prompt_for_settings())
        if yes("Advanced driver settings"):
            for key, value in list(options.items()):
                if key != "driver" and isinstance(value, str):
                    options[key] = ask(key, value)
    except (ImportError, AttributeError, KeyError, ValueError) as exc:
        raise ConfigError("Driver configuration unavailable") from exc
    options["driver"] = module
    return {
        **existing,
        "Station": {
            "latitude": "0",
            "longitude": "0",
            "altitude": ["0", "meter"],
            **existing.get("Station", {}),
            "station_type": name,
        },
        name: options,
    }


def preview_upload(config, samples):
    spools = open_spools(config)
    try:
        for spool in spools:
            sample = samples[spool.station.key]
            spool.append(
                event_from_loop(
                    {
                        "dateTime": sample["dateTime"],
                        "usUnits": sample["usUnits"],
                        **sample["data"],
                    },
                    spool.station_id,
                    spool.get_meta("driver_module"),
                )
            )
        # The same durable uploader handles the first upload and all subsequent retries.
        uploader = Uploader(config, spools, HTTPSClient(config))
        confirmed = uploader.tick()
        for spool in spools:
            if confirmed and spool.get_meta("last_success"):
                print(f"{spool.station.key}: upload OK")
            else:
                print(f"{spool.station.key}: {spool.get_meta('upload_error') or 'queued'}")
        if any(spool.get_meta("upload_error") == "pending" for spool in spools):
            print("Adopt the station in the destination's station list.")
    finally:
        for spool in spools:
            spool.close()


def setup(path, state_dir="/var/lib/weewx-php-ingest"):
    path = Path(path).resolve()
    existing = read_weewx(path).dict() if path.exists() else initial_data(state_dir)
    # Preserve generated identities and tokens when reopening the assistant.
    data = dict(existing)
    ingest = data["Ingest"] = dict(existing.get("Ingest", {}))
    while True:
        try:
            ingest["url"] = domain_url(ask("Destination domain", ingest.get("url")))
            break
        except ConfigError as error:
            print(error)
    ingest.setdefault("collector_id", str(uuid.uuid4()))
    if not any(k in ingest for k in ("token", "token_file", "token_env")):
        ingest["token"] = secrets.token_hex(32)
    ingest.setdefault("state_dir", state_dir)
    ingest.setdefault("send_interval", "10")
    if "Stations" in existing:
        instances = dict(existing["Stations"])
    else:
        key = ingest.pop("station_key", "station")
        instances = {key: {k: v for k, v in existing.items() if k not in ("Ingest", "Stations")}}
        station_options = {}
        for option in (
            "label",
            "silence_timeout",
            "startup_timeout",
            "max_events",
            "min_free_bytes",
            "lifecycle_interval",
            "lifecycle_delay",
            "exclude_fields",
            "python",
            "python_paths",
            "Services",
            "spool_max_bytes",
        ):
            if option in ingest:
                station_options["max_bytes" if option == "spool_max_bytes" else option] = (
                    ingest.pop(option)
                )
        instances[key]["Ingest"] = station_options
        if not existing.get("Ingest", {}).get("url"):
            instances[key]["Station"] = dict(instances[key].get("Station", {}))
            instances[key]["Station"].pop("station_type", None)
    samples = {}
    keys = list(instances)
    for key in keys:
        print(f"Station: {key}")
        while True:
            instances[key] = select_hardware(instances[key])
            candidate = {
                **{k: v for k, v in existing.items() if k in ("WEEWX_ROOT", "USER_ROOT")},
                "Ingest": ingest,
                "Stations": instances,
            }
            with tempfile.TemporaryDirectory(prefix=".setup-", dir=path.parent) as work:
                temporary = Path(work) / "weewx.conf"
                # Resolve relative roots and credentials against the real config directory.
                candidate["Ingest"] = {
                    **ingest,
                    "state_dir": str((path.parent / ingest["state_dir"]).resolve()),
                }
                for option in ("token_file", "ca_file"):
                    if option in ingest:
                        candidate["Ingest"][option] = str((path.parent / ingest[option]).resolve())
                write_config(temporary, candidate)
                account = service_account()
                if account:
                    os.chown(work, account.pw_uid, account.pw_gid)
                    os.chown(temporary, account.pw_uid, account.pw_gid)
                print("Testing hardware (up to 60 seconds)")
                try:
                    samples[key] = probe_packet(temporary, key, timeout=60, account=account)
                    print(f"Hardware OK: {len(samples[key]['data'])} observations")
                    break
                except ConfigError as error:
                    print(error)
                    if not yes("Retry hardware setup", True):
                        raise ConfigError("Setup cancelled; configuration unchanged") from error
        if key == keys[-1] and yes("Add another station"):
            while True:
                new_key = ask("Station key", f"station{len(keys) + 1}")
                if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", new_key) and new_key not in (
                    *keys,
                    "collector",
                    "uploader",
                ):
                    break
                print("Invalid or duplicate station key")
            keys.append(new_key)
            instances[new_key] = {
                k: v for k, v in existing.items() if k in ("WEEWX_ROOT", "USER_ROOT")
            }
    if len(instances) == 1:
        key, one = next(iter(instances.items()))
        options = dict(one.pop("Ingest", {}))
        if "max_bytes" in options:
            options["spool_max_bytes"] = options.pop("max_bytes")
        data = {**one, "Ingest": {**ingest, **options, "station_key": key}}
    else:
        data = {k: v for k, v in existing.items() if k in ("WEEWX_ROOT", "USER_ROOT")}
        data.update({"Ingest": ingest, "Stations": instances})
    write_config(path, data, replace=path.exists())
    config = load_config(path)
    print(f"Configuration: {path}")
    try:
        with service_user():
            preview_upload(config, samples)
    except (OSError, TransportError, ConfigError):
        print("Upload unavailable; readings remain queued")


@contextlib.contextmanager
def service_user():
    account = service_account()
    old_uid, old_gid = (os.geteuid(), os.getegid()) if account else (None, None)
    try:
        if account:
            os.setegid(account.pw_gid)
            os.seteuid(account.pw_uid)
        yield
    finally:
        if account:
            os.seteuid(old_uid)
            os.setegid(old_gid)


def configure(path, state_dir):
    managed = (
        os.name == "posix"
        and os.geteuid() == 0
        and Path("/etc/systemd/system/weewx-php-ingest.service").is_file()
    )
    was_active = False
    if managed:
        was_active = subprocess.call(["systemctl", "is-active", "--quiet", "weewx-php-ingest"]) == 0
        subprocess.run(["systemctl", "stop", "weewx-php-ingest"], check=True)
    succeeded = False
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        setup(path, state_dir)
        succeeded = True
    finally:
        if managed and (succeeded or was_active):
            subprocess.run(["systemctl", "enable", "--now", "weewx-php-ingest"], check=True)
