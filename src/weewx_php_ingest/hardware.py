"""Local USB/serial inventory and bounded, isolated driver tests."""

import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import ConfigError, load_config
from .runtime import create_engine, driver_config

USB_DRIVERS = {
    (0x1941, 0x8021): "FineOffsetUSB",
    (0x0FDE, 0xCA01): "WMR100",
    (0x0FDE, 0xCA08): "WMR300",
    (0x24C0, 0x0003): "AcuRite",
    (0x1130, 0x6801): "TE923",
    (0x6666, 0x5555): "WS28xx",
}


def drivers():
    import weewx.drivers

    found = []
    for path in sorted(Path(weewx.drivers.__file__).parent.glob("*.py")):
        values = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = node.value.value
        if isinstance(values.get("DRIVER_NAME"), str):
            found.append((values["DRIVER_NAME"], "weewx.drivers." + path.stem))
    return sorted(found)


def scan():
    from serial.tools import list_ports

    devices, warnings, seen = [], [], set()
    stable_ports = {os.path.realpath(p): str(p) for p in Path("/dev/serial/by-id").glob("*")}
    try:
        ports = sorted(
            list_ports.comports(include_links=True),
            key=lambda p: (not p.device.startswith("/dev/serial/by-id/"), p.device),
        )
        for port in ports:
            resolved = os.path.realpath(port.device)
            if resolved in seen:
                continue
            seen.add(resolved)
            devices.append(
                {
                    "kind": "serial",
                    "path": stable_ports.get(resolved, port.device),
                    "description": port.description,
                    "driver": None,
                }
            )
    except OSError:
        warnings.append("Serial scan unavailable")
    try:
        import usb.core
    except ImportError:
        warnings.append("USB scan unavailable; select hardware manually")
        return devices, warnings
    try:
        for device in usb.core.find(find_all=True):
            pair = (device.idVendor, device.idProduct)
            devices.append(
                {
                    "kind": "usb",
                    "path": f"{pair[0]:04x}:{pair[1]:04x}",
                    "description": f"USB {device.bus}:{device.address}",
                    "driver": USB_DRIVERS.get(pair),
                }
            )
    except (OSError, NotImplementedError, usb.core.USBError):
        warnings.append("USB scan unavailable; select hardware manually")
    return devices, warnings


def service_account():
    if os.name == "posix" and os.geteuid() == 0:
        import pwd

        try:
            return pwd.getpwnam("weewx-ingest")
        except KeyError:
            pass
    return None


def probe_packet(path, key, timeout=30, account=None):
    # A wedged USB/serial read must never hang the setup process.
    with tempfile.TemporaryDirectory(prefix="weewx-probe-") as work:
        result = Path(work) / "result.json"
        identity = {}
        if account:
            os.chown(work, account.pw_uid, account.pw_gid)
            identity = {
                "user": account.pw_uid,
                "group": account.pw_gid,
                "extra_groups": os.getgrouplist(account.pw_name, account.pw_gid),
            }
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(Path(__file__).resolve().parent.parent), env.get("PYTHONPATH")])
        )
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "weewx_php_ingest",
                    "--config",
                    str(path),
                    "_probe",
                    key,
                    str(result),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                **identity,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConfigError("Hardware test timed out") from exc
        if completed.returncode or not result.is_file():
            raise ConfigError(
                "Hardware test failed; check connection, permissions and driver settings"
            )
        return json.loads(result.read_text())


def run_probe(path, key, destination):
    import weeutil.startup
    import weewx

    config = load_config(path)
    station = next(s for s in config.stations if s.key == key)
    for extra in reversed(station.python_paths):
        sys.path.insert(0, str(extra))
    raw, module = driver_config(station)
    packets = []

    class Probe:
        station_id = "11111111-1111-4111-8111-111111111111"

        def get_meta(self, _key):
            return module

        def append(self, packet):
            packets.append(packet)

    engine = None
    try:
        weeutil.startup.initialize(raw)
        engine = create_engine(raw, station, Probe(), lambda: bool(packets))
        engine.run()
    except weewx.StopNow:
        pass
    finally:
        if engine:
            engine.shutDown()
    if not packets:
        raise ConfigError("No hardware reading received")
    Path(destination).write_text(json.dumps(packets[0]), encoding="utf-8")
    return 0
