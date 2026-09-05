"""Strict native v1/v2 serialization. Source events never change on retry."""

import json
import math
import re
import uuid

MAX_BYTES = 262144
MAX_PACKETS = 128
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\Z", re.ASCII)
FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}\Z")
RESERVED = {"datetime", "usunits", "interval", "source", "sender", "identity", "driver", "kind"}


class ProtocolError(ValueError):
    pass


def valid_uuid(value):
    return isinstance(value, str) and UUID.fullmatch(value) is not None


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def event_from_loop(packet, station_id, driver_module, exclude_fields=()):
    if not valid_uuid(station_id):
        raise ProtocolError("invalid_station_id")
    if not MODULE.fullmatch(driver_module) or len(driver_module) > 160:
        raise ProtocolError("invalid_driver_module")
    timestamp, units = packet.get("dateTime"), packet.get("usUnits")
    if type(timestamp) is not int or not 1 <= timestamp <= 253402300799:
        raise ProtocolError("invalid_timestamp")
    if type(units) is not int or units not in (1, 16, 17):
        raise ProtocolError("invalid_units")
    data = {
        k: v
        for k, v in packet.items()
        if k not in ("dateTime", "usUnits") and k not in exclude_fields
    }
    if not 1 <= len(data) <= 256:
        raise ProtocolError("invalid_field_count")
    for name, value in data.items():
        if not isinstance(name, str) or not FIELD.fullmatch(name) or name.lower() in RESERVED:
            raise ProtocolError("invalid_field")
        if value is not None:
            try:
                valid = type(value) in (int, float) and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                raise ProtocolError("invalid_value")
    return {
        "station_id": station_id,
        "event_id": str(uuid.uuid4()),
        "driver_module": driver_module,
        "kind": "loop",
        "dateTime": timestamp,
        "usUnits": units,
        "data": data,
    }


def event_from_archive(record, station_id, driver_module, exclude_fields=()):
    """Original logger aggregate. Its UUID survives redelivery by the hardware."""
    interval = record.get("interval")
    if (
        type(interval) not in (int, float)
        or not 1 / 60 <= interval <= 1440
        or abs(interval * 60 - round(interval * 60)) > 0.000001
    ):
        raise ProtocolError("invalid_interval")
    event = event_from_loop(
        {k: v for k, v in record.items() if k != "interval"},
        station_id,
        driver_module,
        exclude_fields,
    )
    seconds = round(interval * 60)
    if event["dateTime"] <= seconds:
        raise ProtocolError("invalid_timestamp")
    event["kind"] = "archive"
    event["interval"] = interval
    event["event_id"] = str(
        uuid.uuid5(uuid.UUID(station_id), f"archive:{driver_module}:{event['dateTime']}:{seconds}")
    )
    return event


def version_for(packets):
    if any("source" in p for p in packets):
        return 3
    return 2 if any(p["kind"] == "archive" for p in packets) else 1


def envelope(collector_id, packets):
    return encode(
        {"version": version_for(packets), "collector_id": collector_id, "packets": packets}
    )


def decode_response(body, packets):
    """Validate the complete ACK before releasing anything, including duplicate IDs."""

    def unique_members(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ProtocolError("duplicate_response_property")
            obj[key] = value
        return obj

    try:
        response = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=unique_members,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("invalid_response") from exc
    if (
        not isinstance(response, dict)
        or type(response.get("version")) is not int
        or response["version"] != version_for(packets)
        or response.get("status") != "ok"
        or not isinstance(response.get("results"), list)
    ):
        raise ProtocolError("invalid_response")
    expected = {p["event_id"]: p["station_id"] for p in packets}
    results = {}
    for result in response["results"]:
        if not isinstance(result, dict):
            raise ProtocolError("invalid_result")
        eid, sid, status = (result.get(k) for k in ("event_id", "station_id", "status"))
        if (
            not isinstance(eid, str)
            or eid not in expected
            or eid in results
            or sid != expected[eid]
            or status not in ("stored", "duplicate", "pending", "rejected")
        ):
            raise ProtocolError("invalid_result")
        reason = result.get("reason")
        if status == "rejected" and reason not in (
            "event_conflict",
            "too_old",
            "future_timestamp",
            "station_blocked",
        ):
            raise ProtocolError("invalid_rejection")
        results[eid] = {"status": status, "reason": reason if status == "rejected" else None}
    # Missing results are left unconfirmed by the uploader.
    return results, response.get("limits", {})
