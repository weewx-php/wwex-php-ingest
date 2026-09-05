"""Common lifecycle for bounded local network receivers."""

import logging
import time
import uuid
from pathlib import Path

from .protocol import ProtocolError, encode, event_from_loop
from .sensor_sources import MODULES, Router, sensor_uuid, source_for
from .spool import SpoolFull


def event(
    receiver_id, source_type, model, identity, channel, timestamp, data, stream, exclude_fields=()
):
    source = source_for(receiver_id, model, identity, channel, source_type=source_type)
    sid = sensor_uuid(source)
    packet = event_from_loop(
        {"dateTime": timestamp, "usUnits": 17, **data}, sid, MODULES[source_type], exclude_fields
    )
    packet["source"] = source
    packet["event_id"] = str(uuid.uuid5(uuid.UUID(sid), f"{stream}:{timestamp}"))
    return packet


def run(receiver_class, config, station, parent, section, stopped, maximum):
    router = Router(config, parent, maximum)
    receiver = receiver_class(section)
    last_warning = 0
    try:
        while not stopped():
            parent.set_meta("last_collected", time.time())
            parent.set_meta("collection_state", "listening")
            try:
                packets = receiver.read(parent.station_id, station.exclude_fields)
                for packet, stream, counters in packets:
                    while not stopped():
                        try:
                            router.append(packet, stream=stream, counters=counters)
                            break
                        except SpoolFull:
                            parent.set_meta("collection_state", "spool_full")
                            parent.set_meta("last_collected", time.time())
                            time.sleep(0.25)
                if packets:
                    parent.set_meta("collection_error", None)
            except (ProtocolError, OSError) as exc:
                reason = str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__
                parent.set_meta("collection_error", reason)
                if time.monotonic() - last_warning >= 10:
                    logging.getLogger(__name__).warning("receiver %s: %s", station.key, reason)
                    last_warning = time.monotonic()
    finally:
        receiver.close()
        router.close()


def probe(receiver_class, section, destination, exclude_fields=()):
    receiver = receiver_class(section)
    try:
        while True:
            try:
                packets = receiver.read("11111111-1111-4111-8111-111111111111", exclude_fields)
            except ProtocolError:
                continue
            if packets:
                packet, stream, counters = packets[0]
                Path(destination).write_bytes(
                    encode({**packet, "_stream": stream, "_counters": counters})
                )
                return 0
    finally:
        receiver.close()
