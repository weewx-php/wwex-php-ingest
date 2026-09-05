"""One rtl_433 receiver, with durable, independently adopted sensor journals."""

import json
import math
import os
import queue
import re
import subprocess
import threading
import time

from weewx.drivers import AbstractConfEditor

from .config import ConfigError
from .protocol import FIELD, ProtocolError, encode, event_from_loop
from .sensor_sources import Router as Router
from .sensor_sources import discover_spools as discover_spools
from .sensor_sources import sensor_uuid as sensor_uuid
from .sensor_sources import source_for as source_for
from .spool import SpoolFull

MODULE = "weewx_php_ingest.sdr"
TEXT = re.compile(r"[\x20-\x7e]{1,64}\Z")
META = {"time", "model", "id", "channel", "protocol", "mic", "mod", "freq", "rssi", "snr", "noise"}
# Explicit units only; unknown numeric observations retain an rtl_ prefix.
FIELDS = {
    "temperature_C": ("outTemp", 1, 0),
    "temperature_F": ("outTemp", 5 / 9, -32 * 5 / 9),
    "humidity": ("outHumidity", 1, 0),
    "pressure_hPa": ("pressure", 1, 0),
    "wind_avg_m_s": ("windSpeed", 1, 0),
    "wind_avg_km_h": ("windSpeed", 1 / 3.6, 0),
    "wind_avg_mi_h": ("windSpeed", 0.44704, 0),
    "wind_max_m_s": ("windGust", 1, 0),
    "wind_max_km_h": ("windGust", 1 / 3.6, 0),
    "wind_max_mi_h": ("windGust", 0.44704, 0),
    "wind_dir_deg": ("windDir", 1, 0),
    "battery_ok": ("batteryStatus", -1, 1),
}


def settings(section):
    try:
        device = str(section.get("device", "0"))
        frequency = int(section.get("frequency", 433920000))
        maximum = int(section.get("max_sensors", 256))
        if (
            not re.fullmatch(r"(?:[0-9]{1,3}|:[A-Za-z0-9_-]{1,64})", device)
            or not 1000000 <= frequency <= 2000000000
            or not 1 <= maximum <= 2000
        ):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ConfigError("invalid RTL433 settings") from exc
    return device, frequency, maximum


def command(section):
    device, frequency, _ = settings(section)
    # Ignore implicit rtl_433.conf files; no shell or extra output destinations.
    return [
        "rtl_433",
        "-c",
        "0",
        "-d",
        device,
        "-f",
        str(frequency),
        "-F",
        "json",
        "-M",
        "time:unix",
        "-C",
        "si",
    ]


def decode(line, receiver_id, now=None, exclude_fields=()):
    now = int(time.time()) if now is None else now

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if len(line) > 65536:
            raise ValueError
        raw = json.loads(
            line,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(raw, dict):
            raise ValueError
        identity = raw.get("id")
        channel = raw.get("channel", "")
        if type(identity) not in (int, str) or type(channel) not in (int, str):
            raise ValueError
        source = source_for(receiver_id, raw.get("model"), str(identity), str(channel))
        timestamp = raw.get("time", now)
        # rtl_433 emits Unix time as text, optionally with fractional seconds.
        if isinstance(timestamp, str) and re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,6})?", timestamp):
            timestamp = float(timestamp)
        if type(timestamp) not in (int, float) or not now - 300 <= timestamp <= now + 60:
            raise ValueError
        data = {}
        for key, value in raw.items():
            if key in META or value is None or isinstance(value, (str, list, dict)):
                continue
            if isinstance(value, bool):
                value = int(value)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError
            name = "rtl_" + key
            if not FIELD.fullmatch(name):
                raise ValueError
            if key in FIELDS:
                name, scale, offset = FIELDS[key]
                value = value * scale + offset
                if name in data:  # Ambiguous units in one decoder output.
                    raise ValueError
            data[name] = value
        event = event_from_loop(
            {"dateTime": int(timestamp), "usUnits": 17, **data},
            sensor_uuid(source),
            MODULE,
            exclude_fields,
        )
        event["source"] = source
        return event
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ProtocolError("invalid_rtl433_packet") from exc


class Receiver:
    """Bounded pipe reader. The owning process must close it on stop or probe timeout."""

    def __init__(self, argv):
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.lines = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        oversized = False
        while not self.closed.is_set():
            line = self.process.stdout.readline(65537)
            if not line:
                break
            if len(line) > 65536 or oversized:
                oversized = not line.endswith(b"\n")
                continue
            while not self.closed.is_set():
                try:
                    self.lines.put(line, timeout=0.25)
                    break
                except queue.Full:
                    pass

    def read(self):
        try:
            return self.lines.get(timeout=0.25)
        except queue.Empty:
            if not self.reader.is_alive():
                raise OSError("rtl433_exited") from None
            return None

    def close(self):
        self.closed.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process.wait(timeout=2)
        self.reader.join(timeout=2)
        self.process.stdout.close()


def run(config, station, parent, section, stopped):
    import logging

    router = Router(config, parent, settings(section)[2])
    receiver = Receiver(command(section))
    last_warning = 0
    try:
        while not stopped():
            parent.set_meta("last_collected", time.time())  # Receiver health, not sensor freshness.
            parent.set_meta("collection_state", "listening")
            line = receiver.read()
            if line is None:
                continue
            try:
                event = decode(line, parent.station_id, exclude_fields=station.exclude_fields)
                while not stopped():
                    try:
                        router.append(event)
                        break
                    except SpoolFull:
                        parent.set_meta("collection_state", "spool_full")
                        parent.set_meta("last_collected", time.time())
                        time.sleep(0.25)
            except ProtocolError as exc:
                parent.set_meta("collection_error", str(exc))
                if time.monotonic() - last_warning >= 10:
                    logging.getLogger(__name__).warning("receiver %s: %s", station.key, exc)
                    last_warning = time.monotonic()
    finally:
        receiver.close()
        router.close()


def probe(section, destination, exclude_fields=()):
    from pathlib import Path

    receiver = Receiver(command(section))
    try:
        while True:
            line = receiver.read()
            if line is None:
                continue
            try:
                event = decode(
                    line, "11111111-1111-4111-8111-111111111111", exclude_fields=exclude_fields
                )
            except ProtocolError:
                continue
            Path(destination).write_bytes(encode(event))
            return 0
    finally:
        receiver.close()


def confeditor_loader():
    return Editor()


class Editor(AbstractConfEditor):
    @property
    def default_stanza(self):
        return (
            "[RTL433]\n    driver = weewx_php_ingest.sdr\n    device = 0\n"
            "    frequency = 433920000\n    max_sensors = 256\n"
        )

    def prompt_for_settings(self):
        return {
            "device": self._prompt("device", "0"),
            "frequency": self._prompt("frequency", "433920000"),
        }
