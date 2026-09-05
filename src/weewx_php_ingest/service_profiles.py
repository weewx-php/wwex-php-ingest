"""Guided configuration for independently installed LOOP enrichment services."""

import getpass
import re
from urllib.parse import urlsplit

CLASSES = {
    "PurpleAir": "user.purple.Purple",
    "AirGradient": "user.airgradient.AirGradient",
    "AirLink": "user.airlink.AirLink",
    "air-Q": "user.airQ_corant.AirqService",
}
AIRGRADIENT_FIELDS = {
    "pm01": "pm1_0",
    "pm02Compensated": "pm2_5",
    "pm10": "pm10_0",
    "rco2": "co2",
    "tvocIndex": "tvocIndex",
    "tvocRaw": "tvocRaw",
    "noxIndex": "noxIndex",
    "noxRaw": "noxRaw",
}


def host(ask, label, default=None, *, allow_port=False):
    while True:
        value = ask(label, default)
        try:
            url = urlsplit("http://" + value)
            if (
                not value.isascii()
                or len(value) > 253
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or value.endswith(":")
                or any(c in value for c in "/\\?#@")
                or not url.hostname
                or (url.port is not None and (not allow_port or not 1 <= url.port <= 65535))
            ):
                raise ValueError
            return value
        except ValueError:
            print("Invalid hostname")


def port(ask, label, default="80"):
    while True:
        value = ask(label, default)
        if len(value) <= 5 and value.isascii() and value.isdecimal() and 1 <= int(value) <= 65535:
            return value
        print("Invalid port")


def configure(data, ask):
    ingest = data.setdefault("Ingest", {})
    services = ingest.setdefault("Services", {})
    existing = services.get("data_services", "user.purple.Purple")
    if isinstance(existing, list):
        existing = ",".join(existing)
    default = next((name for name, cls in CLASSES.items() if cls == existing), existing)
    print("Services: PurpleAir, AirGradient, AirLink, air-Q, or Python class")
    while True:
        choice = ask("Service", default)
        choice = next(
            (cls for name, cls in CLASSES.items() if name.lower() == choice.lower()), choice
        )
        classes = [s.strip() for s in choice.split(",")]
        if all(
            len(s) <= 160 and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", s, re.ASCII)
            for s in classes
        ):
            break
        print("Invalid service class")
    services["data_services"] = classes
    data.setdefault("StdArchive", {})["record_generation"] = "software"
    for name, section in (
        ("PurpleAir", "Purple"),
        ("AirGradient", "AirGradient"),
        ("AirLink", "AirLink"),
    ):
        if CLASSES[name] not in classes:
            continue
        settings = data.setdefault(section, {})
        sensor = settings.setdefault("Sensor1", {})
        sensor["hostname"] = host(ask, f"{name} hostname", sensor.get("hostname"))
        sensor["port"] = port(ask, f"{name} port", sensor.get("port", "80"))
        sensor["enable"] = "true"
        if name == "AirGradient" and not settings.get("LoopFields"):
            settings["LoopFields"] = dict(AIRGRADIENT_FIELDS)
    if CLASSES["air-Q"] in classes:
        configure_airq(data, ask)


def configure_airq(data, ask):
    ingest = data["Ingest"]
    services = ingest["Services"]
    prep = services.get("prep_services", [])
    prep = prep if isinstance(prep, list) else [prep] if prep else []
    services["prep_services"] = list(dict.fromkeys([*prep, "user.airQ_corant.AirqUnits"]))
    settings = data.setdefault("airQ", {})
    devices = [s for s in settings.values() if isinstance(s, dict)]
    sensor = devices[0] if devices else settings.setdefault("Sensor1", {})
    sensor["host"] = host(ask, "air-Q host (optional :port)", sensor.get("host"), allow_port=True)
    while True:
        label = "air-Q password (Enter to keep): " if sensor.get("password") else "air-Q password: "
        password = getpass.getpass(label) or sensor.get("password", "")
        if 1 <= len(password.encode("utf-8")) <= 32 and not any(c in password for c in "\r\n\0"):
            sensor["password"] = password
            break
        print("Password must contain 1–32 UTF-8 bytes")
    excluded = ingest.get("exclude_fields", [])
    excluded = excluded if isinstance(excluded, list) else [excluded] if excluded else []
    # Upstream's text/status fields are not measurements. Its no2 is a fraction,
    # while PHP's standard no2 means mass concentration; keep no2_m for explicit mapping.
    for device in [s for s in settings.values() if isinstance(s, dict)]:
        prefix = device.get("prefix")
        for field in ("airqDeviceID", "airqStatus", "airqBattery", "no2"):
            excluded.append(prefix + "_" + field.replace("airq", "") if prefix else field)
    ingest["exclude_fields"] = list(dict.fromkeys(excluded))
