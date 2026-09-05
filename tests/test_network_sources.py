"""Real LAN sockets, multi-device journals and PHP adoption for gateway sources."""

import dataclasses
import json
import socket
import socketserver
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

import pytest
from configobj import ConfigObj

from weewx_php_ingest import gw1000 as gw
from weewx_php_ingest import weatherflow as wf
from weewx_php_ingest.config import ConfigError, load_config
from weewx_php_ingest.hardware import probe_packet
from weewx_php_ingest.protocol import ProtocolError, envelope
from weewx_php_ingest.sensor_sources import Router, discover_spools
from weewx_php_ingest.supervisor import Supervisor, open_spools
from weewx_php_ingest.transport import HTTPSClient
from weewx_php_ingest.uploader import Uploader

RID = "11111111-1111-4111-8111-111111111111"
MAC = bytes.fromhex("aabbccddeeff")


def frame(command, data):
    width = gw.COMMANDS[command]
    body = bytes([command]) + (len(data) + width + 2).to_bytes(width, "big") + data
    return b"\xff\xff" + body + bytes([sum(body) & 255])


def sensor(address, identity, signal=4):
    return bytes([address]) + identity.to_bytes(4, "big") + bytes([1, signal])


def tempest(serial="ST-1", timestamp=None, kind="obs_st"):
    timestamp = int(time.time()) if timestamp is None else timestamp
    message = {"serial_number": serial, "type": kind, "hub_sn": "HB-1"}
    if kind == "rapid_wind":
        message["ob"] = [timestamp, 3, 180]
    else:
        message["obs"] = [
            [timestamp, 1, 2, 4, 180, 3, 1000, 22, 60, 1000, 1, 80, 0.2, 1, 4, 2, 2.5, 1]
        ]
    return json.dumps(message).encode()


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def gateway():
    state = {
        "inventory": sensor(6, 42) + sensor(7, 43),
        "values": bytes.fromhex("0100c80632082710091f401a00d21b012c22332344"),
        "commands": [],
        "corrupt": False,
    }

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            request = b""
            while len(request) < 5:
                part = self.request.recv(5 - len(request))
                if not part:
                    return
                request += part
            assert request[:2] == b"\xff\xff" and request[3] == 3
            assert sum(request[2:-1]) & 255 == request[-1]
            command = request[2]
            state["commands"].append(command)
            payload = {
                0x26: MAC,
                0x3C: state["inventory"],
                0x27: state["values"],
                0x57: bytes.fromhex("80000a86000003e8"),
            }[command]
            output = frame(command, payload)
            if state["corrupt"]:
                output = output[:-1] + bytes([output[-1] ^ 1])
            try:
                for chunk in (output[:2], output[2:5], output[5:]):
                    self.request.sendall(chunk)
            except OSError:
                pass

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    yield {**state, "state": state, "host": "127.0.0.1", "port": server.server_address[1]}
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def network_config(make_config, gateway):
    config = make_config(count=2)
    raw = ConfigObj(str(config.path), interpolation=False)
    for key, name, options in (
        (
            "s0",
            "GW1000",
            {
                "driver": gw.MODULE,
                "host": gateway["host"],
                "port": gateway["port"],
                "poll_interval": 1,
            },
        ),
        (
            "s1",
            "WeatherFlowUDP",
            {"driver": wf.MODULE, "bind": "127.0.0.1", "port": free_udp_port()},
        ),
    ):
        raw["Stations"][key]["Station"]["station_type"] = name
        raw["Stations"][key][name] = options
    raw.write()
    return load_config(config.path)


def options(config, key, name):
    return ConfigObj(str(config.path), interpolation=False)["Stations"][key][name]


@contextmanager
def broadcast(port, payloads):
    stopped = threading.Event()

    def send():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            while not stopped.is_set():
                for payload in payloads:
                    sock.sendto(payload, ("127.0.0.1", port))
                stopped.wait(0.05)

    thread = threading.Thread(target=send)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=2)


def test_gateway_socket_snapshot_identity_and_replacement(gateway):
    receiver = gw.Receiver(gateway)
    rows = receiver.read(RID)
    assert len(rows) == 3
    by_id = {packet["source"]["sensor_id"]: packet for packet, _, _ in rows}
    first = by_id[MAC.hex() + "/0000002a"]
    second = by_id[MAC.hex() + "/0000002b"]
    assert first["data"]["outTemp"] == 21 and second["data"]["outTemp"] == 30
    assert first["source"]["channel"] == "1" and second["source"]["channel"] == "2"
    assert by_id[MAC.hex()]["data"]["inTemp"] == 20
    assert by_id[MAC.hex()]["data"]["pressure"] == 1000
    assert gateway["state"]["commands"] == [0x26, 0x3C, 0x27, 0x3C]
    receiver.next_read = 0
    assert [p["station_id"] for p, _, _ in receiver.read(RID)] == [
        p["station_id"] for p, _, _ in rows
    ]
    gateway["state"]["inventory"] = sensor(6, 44) + sensor(7, 43, signal=0)
    receiver.next_read = 0
    replaced = receiver.read(RID)
    assert len(replaced) == 2
    assert replaced[0][0]["station_id"] != first["station_id"]
    gateway["state"]["corrupt"] = True
    receiver.next_read = 0
    with pytest.raises(ProtocolError, match="frame"):
        receiver.read(RID)


def test_gateway_channel_families_and_ambiguous_arrays():
    addresses = [0, 2, 3, 0x0E, 0x16, 0x1A, 0x1B, 0x1F, 0x27, 0x28, 0x30]
    sensors = gw.inventory(frame(0x3C, b"".join(sensor(a, a + 10) for a in addresses)))
    values = {
        "outtemp": 20,
        "windspeed": 5,
        "soilmoist1": 60,
        "soiltemp1": 15,
        "pm251": 2.5,
        "leak1": 1,
        "temp9": 9,
        "co2": 700,
        "temp17": 19,
        "leafwet1": 34,
        "lightningcount": 40,
        "t_raintotals": 100,
        "p_rainyear": 200,
    }
    rows = gw.split(values, sensors, MAC.hex(), RID)
    by_model = {p["source"]["model"]: p for p, _, _ in rows}
    assert by_model["GW1000"]["data"]["gw_outtemp"] == 20
    assert "outTemp" not in by_model["WH24/WH65"]["data"]
    assert "outTemp" not in by_model["WS80"]["data"]
    assert by_model["WH51"]["data"]["gw_soilMoisturePercent"] == 60
    assert "soilMoist1" not in by_model["WH51"]["data"]
    assert by_model["WH41"]["data"]["pm2_5"] == 2.5
    assert by_model["WH55"]["data"]["gw_leak"] == 1
    assert by_model["WN34"]["data"]["outTemp"] == 9
    assert by_model["WH45/WH46"]["data"]["co2"] == 700
    assert by_model["WN35"]["data"]["gw_leafWetnessPercent"] == 34
    assert by_model["WH57"]["data"]["gw_lightningcount"] == 40
    assert "lightning_strike_count" not in by_model["WH57"]["data"]
    assert by_model["WS90"]["data"]["gw_p_rainyear"] == 200


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        frame(0x3C, sensor(6, 1)[:-1]),
        frame(0x3C, sensor(6, 1) * 2),
        frame(0x3C, sensor(99, 1)),
        frame(0x3C, sensor(6, 1, signal=255)),
    ],
)
def test_bad_gateway_inventory(raw):
    with pytest.raises(ProtocolError):
        gw.inventory(raw)


@pytest.mark.parametrize("raw", [b"\x01\x00", b"\xfe", b"\x06\x32\x06\x33"])
def test_bad_gateway_fields(raw):
    with pytest.raises(ProtocolError):
        gw.parse_values(frame(0x27, raw))


def test_gateway_discovery_uses_valid_peer_and_frame():
    port = free_udp_port()
    payload = MAC + socket.inet_aton("127.0.0.1") + (45000).to_bytes(2, "big") + b"\x00GW1000"
    with broadcast(port, [frame(0x12, payload), b"invalid"]):
        assert gw.discover(timeout=0.2, port=port) == [("127.0.0.1", 45000)]


def test_weatherflow_real_udp_two_devices_and_silence():
    port = free_udp_port()
    receiver = wf.Receiver({"bind": "127.0.0.1", "port": port})
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for serial in ("ST-1", "ST-2"):
                sender.sendto(tempest(serial), ("127.0.0.1", port))
        one = receiver.read(RID)[0][0]
        two = receiver.read(RID)[0][0]
        assert one["station_id"] != two["station_id"]
        assert one["data"]["outTemp"] == 22 and one["data"]["rain"] == 0.2
        assert one["data"]["wf_windAverage"] == 2 and "windSpeed" not in one["data"]
        assert receiver.read(RID) == []
    finally:
        receiver.close()


def test_weatherflow_stream_dedup_restart_and_rain(network_config):
    spools = open_spools(network_config)
    parent = spools[1]
    router = Router(network_config, parent)
    now = int(time.time())
    try:
        rapid, stream, _ = wf.decode(tempest(timestamp=now, kind="rapid_wind"), parent.station_id)[
            0
        ]
        obs, other, _ = wf.decode(tempest(timestamp=now - 3), parent.station_id)[0]
        assert rapid["station_id"] == obs["station_id"]
        assert router.append(rapid, stream=stream)
        assert router.append(obs, stream=other)  # Late minute observation after rapid wind.
        assert not router.append(obs, stream=other)
        router.close()
        router = Router(network_config, parent)
        assert not router.append(obs, stream=other)
        discover_spools(network_config, spools)
        child = next(s for s in spools if s.get_meta("source"))
        events = [
            json.loads(r[0]) for r in child.db.execute("SELECT payload FROM events ORDER BY seq")
        ]
        assert len(events) == 2
        assert sum(e["data"].get("rain", 0) for e in events) == 0.2
    finally:
        router.close()
        for spool in spools:
            spool.close()


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{}",
        pytest.param(b"x" * 32769, id="oversized"),
        b'{"type":"obs_st","type":"rapid_wind"}',
        tempest().replace(b'"ST-1"', b'"AR-1"'),
        tempest().replace(b"22,", b"NaN,"),
        tempest().replace(b"22,", b"true,"),
        tempest(timestamp=1),
    ],
)
def test_invalid_weatherflow(payload):
    with pytest.raises(ProtocolError):
        wf.decode(payload, RID)


def test_weatherflow_filter_air_sky_and_status():
    assert wf.decode(tempest(), RID, hub_serial="HB-2") == []
    assert wf.decode(b'{"type":"hub_status","serial_number":"HB-1"}', RID) == []
    now = int(time.time())
    for serial, kind, row in (
        ("AR-1", "obs_air", [now, 1000, 20, 50, 3, 5, 3.4, 1]),
        ("SK-1", "obs_sky", [now, 1000, 2, 0.1, 1, 2, 3, 180, 3.2, 1, 80, None, 1, 3]),
    ):
        result = wf.decode(
            json.dumps({"serial_number": serial, "type": kind, "hub_sn": "HB-1", "obs": [row]}), RID
        )
        assert len(result) == 1 and result[0][0]["source"]["sensor_id"] == serial


def test_guided_network_probes(network_config):
    gw_packet = probe_packet(network_config.path, "s0", timeout=5)
    assert gw_packet["source"]["type"] == "gw1000" and gw_packet["_stream"] == "snapshot"
    port = int(options(network_config, "s1", "WeatherFlowUDP")["port"])
    with broadcast(port, [tempest()]):
        wf_packet = probe_packet(network_config.path, "s1", timeout=5)
    assert wf_packet["source"]["type"] == "weatherflow_udp"
    assert not network_config.state_dir.exists()


def test_duplicate_udp_listeners_rejected(network_config):
    raw = ConfigObj(str(network_config.path), interpolation=False)
    raw["Stations"]["s0"] = raw["Stations"]["s1"].dict()
    raw.write()
    with pytest.raises(ConfigError, match="only once"):
        open_spools(load_config(network_config.path))


def test_gateway_counter_baseline_is_durable(network_config):
    spools = open_spools(network_config)
    parent = spools[0]
    sensors = gw.inventory(frame(0x3C, sensor(3, 42)))
    router = Router(network_config, parent)
    now = int(time.time()) - 10
    try:
        for timestamp, total in ((now, 100), (now + 1, 100.2), (now + 2, 1)):
            packet, stream, counters = gw.split(
                {"t_raintotals": total}, sensors, MAC.hex(), parent.station_id, timestamp=timestamp
            )[0]
            router.append(packet, stream=stream, counters=counters)
            router.close()
            router = Router(network_config, parent)
        discover_spools(network_config, spools)
        events = [
            json.loads(r[0])
            for r in spools[-1].db.execute("SELECT payload FROM events ORDER BY seq")
        ]
        assert "rain" not in events[0]["data"] and "rain" not in events[2]["data"]
        assert events[1]["data"]["rain"] == pytest.approx(0.2)
    finally:
        router.close()
        for spool in spools:
            spool.close()


def test_real_lan_to_php_independent_adoption(network_config, tmp_path, tls_server):
    from php_receiver import receiver

    config = dataclasses.replace(
        network_config, endpoint=tls_server["url"], ca_file=tls_server["ca"]
    )
    spools = open_spools(config)
    routers = [Router(config, parent) for parent in spools]
    gw_receiver = gw.Receiver(options(config, "s0", "GW1000"))
    wf_receiver = wf.Receiver(options(config, "s1", "WeatherFlowUDP"))
    port = int(options(config, "s1", "WeatherFlowUDP")["port"])
    events = []
    try:
        events.extend(gw_receiver.read(spools[0].station_id))
        for packet, stream, counters in events:
            routers[0].append(packet, stream=stream, counters=counters)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for serial in ("ST-1", "ST-2"):
                sender.sendto(tempest(serial), ("127.0.0.1", port))
        for _ in range(2):
            packet, stream, counters = wf_receiver.read(spools[1].station_id)[0]
            routers[1].append(packet, stream=stream, counters=counters)
            events.append((packet, stream, counters))
        client = HTTPSClient(config)
        uploader = Uploader(config, spools, client)
        now = time.time()
        with receiver(tmp_path, tls_server) as (cli, directory):
            assert uploader.tick(now) == 0
            lines = cli("list").splitlines()
            assert len(lines) == 5
            assert all(s.status()["events"] == 0 for s in spools[:2])
            with sqlite3.connect(directory / "live.sdb") as db:
                assert db.execute("SELECT count(*) FROM packet").fetchone()[0] == 0
            cli("adopt", lines[0].split()[0])
            assert uploader.tick(now + 61) == 1
            for line in lines[1:]:
                cli("adopt", line.split()[0])
            tls_server["behaviors"].append("lost")
            assert uploader.tick(now + 122) == 0
            assert uploader.tick(now + 184) == 4
            with sqlite3.connect(directory / "live.sdb") as db:
                rows = db.execute("SELECT identity,data FROM packet").fetchall()
                assert len(rows) == 5 and len({row[0] for row in rows}) == 5
                assert {
                    json.loads(r[0])["type"] for r in db.execute("SELECT source FROM weewx_sensor")
                } == {"gw1000", "weatherflow_udp"}
            assert all(s.status()["events"] == 0 for s in spools)
            bad = {**events[0][0], "driver_module": wf.MODULE}
            assert client.send(envelope(config.collector_id, [bad])).status == 400
            bad = {**events[0][0], "station_id": str(uuid.uuid4())}
            assert client.send(envelope(config.collector_id, [bad])).status == 400
    finally:
        wf_receiver.close()
        gw_receiver.close()
        for router in routers:
            router.close()
        for spool in spools:
            spool.close()


def test_supervised_network_workers_discover_and_stop(network_config):
    supervisor = Supervisor(network_config)
    port = int(options(network_config, "s1", "WeatherFlowUDP")["port"])
    try:
        with broadcast(port, [tempest("ST-1"), tempest("ST-2")]):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                supervisor.poll()
                if (
                    len(supervisor.spools[0].get_meta("sensors", {})) == 3
                    and len(supervisor.spools[1].get_meta("sensors", {})) == 2
                ):
                    break
                time.sleep(0.05)
        assert len(supervisor.children) == 3
        assert len(supervisor.spools[0].get_meta("sensors", {})) == 3
        assert len(supervisor.spools[1].get_meta("sensors", {})) == 2
        assert all(parent.status()["events"] == 0 for parent in supervisor.spools)
    finally:
        supervisor.shutdown()
    assert all(c.process is None or c.process.poll() is not None for c in supervisor.children)


def test_gateway_read_deadline_is_bounded():
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(5)
            time.sleep(0.4)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            gw.request("127.0.0.1", server.server_address[1], 0x26, 0.1)
        assert time.monotonic() - started < 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_gateway_inventory_swap_discards_snapshot(gateway, monkeypatch):
    original = gw.request
    reads = 0

    def request(*args):
        nonlocal reads
        if args[2] == 0x3C:
            reads += 1
            if reads == 2:
                gateway["state"]["inventory"] = sensor(6, 99)
        return original(*args)

    monkeypatch.setattr(gw, "request", request)
    with pytest.raises(ProtocolError, match="inventory_changed"):
        gw.Receiver(gateway).read(RID)


def test_weatherflow_capacity_conflict_and_exclusions(network_config):
    spools = open_spools(network_config)
    router = Router(network_config, spools[1], maximum=1)
    now = int(time.time())
    try:
        packet, stream, _ = wf.decode(
            tempest(timestamp=now), spools[1].station_id, exclude_fields=("rain",)
        )[0]
        assert "rain" not in packet["data"]
        router.append(packet, stream=stream)
        bad = {**packet, "data": {**packet["data"], "outTemp": 99}}
        with pytest.raises(ProtocolError, match="conflict"):
            router.append(bad, stream=stream)
        other, stream, _ = wf.decode(tempest("ST-2", timestamp=now), spools[1].station_id)[0]
        with pytest.raises(ProtocolError, match="capacity"):
            router.append(other, stream=stream)
        assert len(spools[1].get_meta("sensors")) == 1
    finally:
        router.close()
        for spool in spools:
            spool.close()


def test_bundled_gateway_parser_checksum():
    import hashlib
    from pathlib import Path

    from weewx_php_ingest import _vendor

    root = Path(_vendor.__file__).parent
    metadata = json.loads((root / "gw1000-source.json").read_text())
    assert hashlib.sha256((root / "gw1000.py").read_bytes()).hexdigest() == metadata["sha256"]
    assert (root / "LICENSE-gw1000").is_file()
