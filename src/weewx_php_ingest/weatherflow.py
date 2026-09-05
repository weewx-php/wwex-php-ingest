"""WeatherFlow local UDP: one journal per Tempest, AIR or SKY serial."""

import ipaddress
import json
import math
import re
import socket
import time

from weewx.drivers import AbstractConfEditor

from . import network_sources
from .config import ConfigError
from .protocol import ProtocolError

MODULE = "weewx_php_ingest.weatherflow"
# Keep interval wind averages separate from instantaneous rapid_wind samples.
LAYOUTS = {
    "rapid_wind": (3, {1: "windSpeed", 2: "windDir"}),
    "obs_air": (
        8,
        {
            1: "pressure",
            2: "outTemp",
            3: "outHumidity",
            4: "lightning_strike_count",
            5: "lightning_distance",
            6: "supplyVoltage",
        },
    ),
    "obs_sky": (
        14,
        {
            1: "luminosity",
            2: "UV",
            3: "rain",
            4: "wf_windLull",
            5: "wf_windAverage",
            6: "windGust",
            7: "wf_windDirection",
            8: "supplyVoltage",
            10: "radiation",
            11: "wf_dayRain",
            12: "wf_precipitationType",
        },
    ),
    "obs_st": (
        18,
        {
            1: "wf_windLull",
            2: "wf_windAverage",
            3: "windGust",
            4: "wf_windDirection",
            6: "pressure",
            7: "outTemp",
            8: "outHumidity",
            9: "luminosity",
            10: "UV",
            11: "radiation",
            12: "rain",
            13: "wf_precipitationType",
            14: "lightning_distance",
            15: "lightning_strike_count",
            16: "supplyVoltage",
        },
    ),
}
MODELS = {"ST": "Tempest", "AR": "AIR", "SK": "SKY"}


def settings(section):
    try:
        bind = str(ipaddress.IPv4Address(section.get("bind", "0.0.0.0")))
        port = int(section.get("port", 50222))
        maximum = int(section.get("max_sensors", 256))
        hub = section.get("hub_serial", "")
        if not 1 <= port <= 65535 or not 1 <= maximum <= 2000:
            raise ValueError
        if not isinstance(hub, str) or (hub and not re.fullmatch(r"HB-[0-9]{1,16}", hub)):
            raise ValueError
        return bind, port, maximum, hub
    except (ValueError, TypeError) as exc:
        raise ConfigError("invalid WeatherFlow UDP settings") from exc


def decode(payload, receiver_id, now=None, exclude_fields=(), hub_serial=""):
    now = int(time.time()) if now is None else now

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        if len(payload) > 32768:
            raise ValueError
        raw = json.loads(
            payload,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            raise ValueError
        kind = raw["type"]
        if kind not in LAYOUTS:
            return []  # Hub/status/event messages contain no observation to accumulate.
        serial = raw.get("serial_number")
        if not isinstance(serial, str) or not re.fullmatch(r"(?:ST|AR|SK)-[0-9]{1,16}", serial):
            raise ValueError
        prefix = serial.split("-")[0]
        if (
            kind != "rapid_wind"
            and prefix != {"obs_air": "AR", "obs_sky": "SK", "obs_st": "ST"}[kind]
        ):
            raise ValueError
        if kind == "rapid_wind" and prefix == "AR":
            raise ValueError
        hub = raw.get("hub_sn")
        if not isinstance(hub, str) or not re.fullmatch(r"HB-[0-9]{1,16}", hub):
            raise ValueError
        if hub_serial and hub != hub_serial:
            return []
        rows = [raw.get("ob")] if kind == "rapid_wind" else raw.get("obs")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
            raise ValueError
        length, mapping = LAYOUTS[kind]
        packets = []
        for row in rows:
            if not isinstance(row, list) or len(row) != length:
                raise ValueError
            timestamp = row[0]
            if type(timestamp) is not int or not now - 300 <= timestamp <= now + 60:
                raise ValueError
            if any(
                v is not None and (type(v) not in (int, float) or not math.isfinite(v))
                for v in row[1:]
            ):
                raise ValueError
            data = {name: row[index] for index, name in mapping.items() if row[index] is not None}
            if "rain" in data and data["rain"] < 0:
                raise ValueError
            if not any(k not in exclude_fields for k in data):
                continue
            packet = network_sources.event(
                receiver_id,
                "weatherflow_udp",
                MODELS[prefix],
                serial,
                "",
                timestamp,
                data,
                kind,
                exclude_fields,
            )
            packets.append((packet, kind, ()))
        return packets
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ProtocolError("invalid_weatherflow_packet") from exc


class Receiver:
    def __init__(self, section):
        bind, port, _, self.hub = settings(section)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.bind((bind, port))
            self.socket.settimeout(0.25)
        except BaseException:
            self.socket.close()
            raise

    def read(self, receiver_id, exclude_fields=()):
        try:
            payload, _ = self.socket.recvfrom(32769)
        except TimeoutError:
            return []
        return decode(payload, receiver_id, exclude_fields=exclude_fields, hub_serial=self.hub)

    def close(self):
        self.socket.close()


def run(config, station, parent, section, stopped):
    network_sources.run(Receiver, config, station, parent, section, stopped, settings(section)[2])


def probe(section, destination, exclude_fields=()):
    return network_sources.probe(Receiver, section, destination, exclude_fields)


def confeditor_loader():
    return Editor()


class Editor(AbstractConfEditor):
    @property
    def default_stanza(self):
        return (
            "[WeatherFlowUDP]\n    driver = weewx_php_ingest.weatherflow\n"
            "    bind = 0.0.0.0\n    port = 50222\n    hub_serial = \n    max_sensors = 256\n"
        )

    def prompt_for_settings(self):
        return {"port": self._prompt("port", "50222"), "hub_serial": self._prompt("hub_serial", "")}
