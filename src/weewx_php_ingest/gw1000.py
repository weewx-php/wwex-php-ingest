"""Read-only Ecowitt LAN API with physical sensor identity from the sensor table."""

import ipaddress
import math
import socket
import time

from weewx.drivers import AbstractConfEditor

from . import network_sources
from ._vendor.gw1000 import ApiParser
from .config import ConfigError
from .protocol import ProtocolError

MODULE = "weewx_php_ingest.gw1000"
# Only read commands. No device configuration or write command is reachable.
COMMANDS = {0x12: 2, 0x26: 1, 0x27: 2, 0x3C: 2, 0x57: 2}
MODELS = {
    0: "WH24/WH65",
    1: "WH68",
    2: "WS80",
    3: "WH40",
    4: "WH25",
    5: "WH26/WH32",
    0x1A: "WH57",
    0x27: "WH45/WH46",
    0x30: "WS90",
    0x31: "WS85",
}
RANGES = (
    (6, 13, "WH31"),
    (14, 21, "WH51"),
    (22, 25, "WH41"),
    (27, 30, "WH55"),
    (31, 38, "WN34"),
    (40, 47, "WN35"),
)
STANDARD = {
    "intemp": "inTemp",
    "inhumid": "inHumidity",
    "absbarometer": "pressure",
    "relbarometer": "barometer",
    "outtemp": "outTemp",
    "outhumid": "outHumidity",
    "winddir": "windDir",
    "windspeed": "windSpeed",
    "gustspeed": "windGust",
    "uvi": "UV",
    "light": "luminosity",
    "t_rainrate": "rainRate",
    "p_rainrate": "rainRate",
}
GROUPS = (
    ((4,), ("intemp", "inhumid", "absbarometer", "relbarometer")),
    ((0, 2, 5, 0x30), ("outtemp", "outhumid", "dewpoint", "windchill", "heatindex")),
    ((0, 1, 2, 0x30, 0x31), ("winddir", "windspeed", "gustspeed", "daymaxwind")),
    ((0, 2, 0x30), ("light", "uv", "uvi")),
    (
        (0, 3),
        (
            "t_rainrate",
            "t_raintotals",
            "t_rainyear",
            "t_rainmonth",
            "t_rainday",
            "t_rainweek",
            "t_rainevent",
        ),
    ),
    (
        (0x30, 0x31),
        ("p_rainrate", "p_rainyear", "p_rainmonth", "p_rainday", "p_rainweek", "p_rainevent"),
    ),
)
COUNTERS = (("gw_t_raintotals", 1), ("gw_t_rainyear", 1), ("gw_p_rainyear", 1))


def settings(section):
    try:
        host = section.get("host", "auto")
        if host != "auto":
            host = str(ipaddress.IPv4Address(host))
        port = int(section.get("port", 45000))
        maximum = int(section.get("max_sensors", 256))
        interval = float(section.get("poll_interval", 10))
        timeout = float(section.get("timeout", 3))
        if (
            not 1 <= port <= 65535
            or not 1 <= maximum <= 2000
            or not 1 <= interval <= 60
            or not 0.1 <= timeout <= 5
        ):
            raise ValueError
        return host, port, maximum, interval, timeout
    except (ValueError, TypeError) as exc:
        raise ConfigError("invalid GW1000 settings") from exc


def frame_payload(frame, command):
    width = COMMANDS[command]
    if len(frame) < 4 + width or frame[:3] != b"\xff\xff" + bytes([command]):
        raise ProtocolError("invalid_gateway_frame")
    size = int.from_bytes(frame[3 : 3 + width], "big")
    if len(frame) != size + 2 or size > 8192 or sum(frame[2:-1]) & 255 != frame[-1]:
        raise ProtocolError("invalid_gateway_frame")
    return frame[3 + width : -1]


def request(host, port, command, timeout):
    width = COMMANDS[command]
    deadline = time.monotonic() + timeout
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.sendall(bytes([255, 255, command, 3, (command + 3) & 255]))

        def read(size):
            result = b""
            while len(result) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
                part = connection.recv(size - len(result))
                if not part:
                    raise ProtocolError("truncated_gateway_frame")
                result += part
            return result

        header = read(3 + width)
        size = int.from_bytes(header[3:], "big")
        if not 2 + width <= size <= 8192:
            raise ProtocolError("invalid_gateway_frame")
        frame = header + read(size + 2 - len(header))
        frame_payload(frame, command)
        return frame


def discover(timeout=5, port=59387):
    """Listen for the gateway's LAN discovery broadcasts; return unique MACs."""
    found = {}
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("0.0.0.0", port))
        while time.monotonic() < deadline:
            listener.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                raw, peer = listener.recvfrom(8195)
                payload = frame_payload(raw, 0x12)
                if not 13 <= len(payload) <= 256:
                    continue
                address = str(ipaddress.IPv4Address(payload[6:10]))
                service_port = int.from_bytes(payload[10:12], "big")
                if address == peer[0] and service_port:
                    found[payload[:6].hex()] = (address, service_port)
            except TimeoutError:
                break
            except (ValueError, ProtocolError):
                continue
    return sorted(found.values())


def parse_values(frame, command=0x27):
    payload = frame_payload(frame, command)
    parser = ApiParser(log_unknown_fields=False)
    structure = parser.live_data_struct if command == 0x27 else parser.rain_data_struct
    index, seen = 0, set()
    while index < len(payload):
        tag = payload[index : index + 1]
        if tag not in structure or tag in seen:
            raise ProtocolError("unsupported_gateway_field")
        seen.add(tag)
        index += 1 + structure[tag][1]
        if index > len(payload):
            raise ProtocolError("truncated_gateway_field")
    return parser.parse_addressed_data(payload, structure)


def inventory(frame):
    payload = frame_payload(frame, 0x3C)
    if len(payload) % 7:
        raise ProtocolError("invalid_gateway_inventory")
    result = {}
    for index in range(0, len(payload), 7):
        address, identity, battery, signal = (
            payload[index],
            payload[index + 1 : index + 5].hex(),
            payload[index + 5],
            payload[index + 6],
        )
        if address in result or signal > 4:
            raise ProtocolError("invalid_gateway_inventory")
        model, channel = MODELS.get(address), ""
        for first, last, name in RANGES:
            if first <= address <= last:
                model, channel = name, str(address - first + 1)
        if model is None:
            raise ProtocolError("unsupported_gateway_sensor")
        result[address] = {
            "id": identity,
            "battery": battery,
            "signal": signal,
            "model": model,
            "channel": channel,
        }
    return {k: v for k, v in result.items() if v["id"] not in ("fffffffe", "ffffffff")}


def split(values, sensors, mac, receiver_id, timestamp=None, exclude_fields=()):
    timestamp = int(time.time()) if timestamp is None else timestamp
    active = {address for address, sensor in sensors.items() if sensor["signal"] > 0}
    data = {
        address: {
            "gw_batteryRaw": sensors[address]["battery"],
            "gw_signal": sensors[address]["signal"],
        }
        for address in active
    }
    gateway = {}

    def put(target, field, name=None):
        value = values.get(field)
        if type(value) in (int, float) and math.isfinite(value):
            target[name or STANDARD.get(field, "gw_" + field)] = value

    for candidates, fields in GROUPS:
        registered = [address for address in candidates if address in sensors]
        if len(registered) == 1:
            if registered[0] in active:
                for field in fields:
                    put(data[registered[0]], field)
        elif not registered and candidates == (4,):
            for field in fields:
                put(gateway, field)  # Integrated gateway temperature/humidity/pressure.
        else:
            # Firmware exposes one selected value, not a value per possible array.
            # Preserve it as gateway aggregate without inventing physical ownership.
            for field in fields:
                put(gateway, field, "gw_" + field)
    for address in active:
        target, channel = data[address], int(sensors[address]["channel"] or 0)
        model = sensors[address]["model"]
        if model == "WH31":
            put(target, f"temp{channel}", "outTemp")
            put(target, f"humid{channel}", "outHumidity")
        elif model == "WH51":
            put(target, f"soiltemp{channel}", "soilTemp1")
            put(target, f"soilmoist{channel}", "gw_soilMoisturePercent")
        elif model == "WH41":
            put(target, f"pm25{channel}", "pm2_5")
            put(target, f"pm25{channel}_24h_avg", "gw_pm2_5_24h")
        elif model == "WH55":
            put(target, f"leak{channel}", "gw_leak")
        elif model == "WN34":
            put(target, f"temp{channel + 8}", "outTemp")
        elif model == "WN35":
            put(target, f"leafwet{channel}", "gw_leafWetnessPercent")
        elif model == "WH57":
            for field in ("lightningdist", "lightningdettime", "lightningcount"):
                put(target, field)
        elif model == "WH45/WH46":
            for field, name in {
                "temp17": "outTemp",
                "humid17": "outHumidity",
                "pm255": "pm2_5",
                "pm10": "pm10_0",
                "pm1": "pm1_0",
                "co2": "co2",
            }.items():
                put(target, field, name)
            for field in (
                "pm4",
                "pm10_24h_avg",
                "pm255_24h_avg",
                "pm1_24h_avg",
                "pm4_24h_avg",
                "co2_24h_avg",
            ):
                put(target, field)
    packets = []
    for address, readings in [*data.items(), (None, gateway)]:
        if not any(k not in exclude_fields for k in readings):
            continue
        sensor = sensors[address] if address is not None else None
        packet = network_sources.event(
            receiver_id,
            "gw1000",
            sensor["model"] if sensor else "GW1000",
            mac + "/" + sensor["id"] if sensor else mac,
            sensor["channel"] if sensor else "",
            timestamp,
            readings,
            "snapshot",
            exclude_fields,
        )
        # Gateway aggregates also use counter baselines, never daily totals as interval rain.
        packets.append((packet, "snapshot", COUNTERS))
    return packets


class Receiver:
    def __init__(self, section):
        self.host, self.port, _, self.interval, self.timeout = settings(section)
        if self.host == "auto":
            found = discover()
            if len(found) != 1:
                raise ConfigError("GW1000 discovery requires exactly one gateway; configure host")
            self.host, self.port = found[0]
        self.next_read = 0

    def read(self, receiver_id, exclude_fields=()):
        if time.monotonic() < self.next_read:
            time.sleep(min(0.25, self.next_read - time.monotonic()))
            return []
        self.next_read = time.monotonic() + self.interval
        mac = frame_payload(request(self.host, self.port, 0x26, self.timeout), 0x26)
        if len(mac) != 6 or mac in (b"\0" * 6, b"\xff" * 6):
            raise ProtocolError("invalid_gateway_mac")
        before = inventory(request(self.host, self.port, 0x3C, self.timeout))
        values = parse_values(request(self.host, self.port, 0x27, self.timeout))
        if any(address in before for address in (0x30, 0x31)):
            values.update(parse_values(request(self.host, self.port, 0x57, self.timeout), 0x57))
        after = inventory(request(self.host, self.port, 0x3C, self.timeout))
        if {k: v["id"] for k, v in before.items()} != {k: v["id"] for k, v in after.items()}:
            raise ProtocolError("gateway_inventory_changed")
        return split(values, after, mac.hex(), receiver_id, exclude_fields=exclude_fields)

    def close(self):
        pass  # Each bounded request owns and closes its TCP connection.


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
            "[GW1000]\n    driver = weewx_php_ingest.gw1000\n    host = auto\n"
            "    port = 45000\n    poll_interval = 10\n    timeout = 3\n    max_sensors = 256\n"
        )

    def prompt_for_settings(self):
        host = self._prompt("host", "auto")
        port = "45000"
        if host == "auto":
            found = discover()
            if not found:
                raise ConfigError("No GW1000 found; enter its IPv4 address")
            choices = {address: str(service_port) for address, service_port in found}
            host = (
                next(iter(choices))
                if len(choices) == 1
                else self._prompt("host", None, list(choices))
            )
            port = choices[host]
        return {"host": host, "port": port}
