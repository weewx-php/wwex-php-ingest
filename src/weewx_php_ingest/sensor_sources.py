"""Shared identity, journals and admission for multi-device sources."""

import hashlib
import re
import uuid
from dataclasses import replace

from .protocol import ProtocolError, encode, valid_uuid
from .spool import Spool

MODULES = {
    "rtl_433": "weewx_php_ingest.sdr",
    "gw1000": "weewx_php_ingest.gw1000",
    "weatherflow_udp": "weewx_php_ingest.weatherflow",
}
TEXT = re.compile(r"[\x20-\x7e]{1,64}\Z")


def source_for(receiver_id, model, sensor_id, channel="", *, source_type="rtl_433"):
    source = {
        "type": source_type,
        "receiver_id": receiver_id,
        "model": model,
        "sensor_id": sensor_id,
        "channel": channel,
    }
    validate_source(source)
    return source


def validate_source(source):
    if (
        not isinstance(source, dict)
        or set(source) != {"type", "receiver_id", "model", "sensor_id", "channel"}
        or not isinstance(source["type"], str)
        or source["type"] not in MODULES
        or not valid_uuid(source["receiver_id"])
        or any(
            not isinstance(source[k], str) or not TEXT.fullmatch(source[k])
            for k in ("model", "sensor_id")
        )
        or not isinstance(source["channel"], str)
        or (source["channel"] != "" and not TEXT.fullmatch(source["channel"]))
    ):
        raise ProtocolError("invalid_sensor_source")


def sensor_uuid(source):
    validate_source(source)
    name = (
        source["type"]
        + ":"
        + encode([source[k] for k in ("model", "sensor_id", "channel")]).decode()
    )
    return str(uuid.uuid5(uuid.UUID(source["receiver_id"]), name))


def child_spool(config, parent, source):
    sid = sensor_uuid(source)
    station = replace(
        parent.station,
        key="sensor-" + sid,
        label=f"{source['model']} {source['sensor_id']} {source['channel']}".strip(),
    )
    path = config.state_dir / "sensors" / parent.station.key / (sid + ".sqlite3")
    spool = Spool(path, config.collector_id, MODULES[source["type"]], station, station_id=sid)
    spool.set_meta("source", source)
    return spool


def discover_spools(config, spools):
    """Refresh uploader/status readers without starting a process per sensor."""
    paths = {s.path for s in spools}
    for parent in list(spools):
        if parent.get_meta("driver_module") not in MODULES.values() or parent.get_meta("source"):
            continue
        for source in parent.get_meta("sensors", {}).values():
            path = (
                config.state_dir
                / "sensors"
                / parent.station.key
                / (sensor_uuid(source) + ".sqlite3")
            )
            if path not in paths:
                spools.append(child_spool(config, parent, source))
                paths.add(path)


class Router:
    def __init__(self, config, parent, maximum=256):
        self.config, self.parent, self.maximum = config, parent, maximum
        self.spools = {}

    def append(self, event, *, stream="radio", counters=()):
        event = {**event, "data": dict(event["data"])}
        source = event["source"]
        sid = sensor_uuid(source)
        if (
            source["receiver_id"] != self.parent.station_id
            or sid != event["station_id"]
            or event["driver_module"] != MODULES[source["type"]]
            or event["driver_module"] != self.parent.get_meta("driver_module")
        ):
            raise ProtocolError("sensor_identity_mismatch")
        registry = self.parent.get_meta("sensors", {})
        if sid not in registry:
            if len(registry) >= self.maximum:
                raise ProtocolError("sensor_capacity")
            registry[sid] = source
            self.parent.set_meta("sensors", registry)
        if sid not in self.spools:
            self.spools[sid] = child_spool(self.config, self.parent, source)
        spool = self.spools[sid]
        fingerprint = hashlib.sha256(encode(dict(sorted(event["data"].items())))).hexdigest()
        state_key = "radio_last" if stream == "radio" else "stream_" + stream
        previous = spool.get_meta(state_key, {})
        timestamp = event["dateTime"]
        if timestamp < previous.get("time", 0):
            raise ProtocolError("out_of_order_sensor_packet")
        if fingerprint == previous.get("digest") and timestamp - previous.get("time", 0) <= (
            2 if stream == "radio" else 0
        ):
            return False
        if stream != "radio" and timestamp == previous.get("time"):
            raise ProtocolError("sensor_event_conflict")
        # Rain totals are not interval amounts. Commit baseline with the queued event.
        metadata = {state_key: {"digest": fingerprint, "time": timestamp}}
        for field, scale in counters or (("rtl_rain_mm", 1), ("rtl_rain_in", 25.4)):
            total = event["data"].get(field)
            if total is not None and total >= 0:
                old = spool.get_meta("rain_counter")
                if (
                    old
                    and old["field"] == field
                    and total >= old["value"]
                    and "rain" not in self.parent.station.exclude_fields
                ):
                    event["data"]["rain"] = (total - old["value"]) * scale
                metadata["rain_counter"] = {"field": field, "value": total}
                break
        spool.append(event, metadata=metadata)
        return True

    def close(self):
        for spool in self.spools.values():
            spool.close()
