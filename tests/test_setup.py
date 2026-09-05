import uuid
from types import SimpleNamespace

import pytest
from configobj import ConfigObj

from weewx_php_ingest.config import ConfigError, load_config, read_token
from weewx_php_ingest.configure import domain_url, initialize, setup, write_config
from weewx_php_ingest.hardware import drivers, probe_packet, scan
from weewx_php_ingest.supervisor import open_spools


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("weather.example", "https://weather.example/ingest/weewx.php"),
        ("https://weather.example/weather/", "https://weather.example/weather/ingest/weewx.php"),
        ("https://weather.example/ingest/weewx.php", "https://weather.example/ingest/weewx.php"),
    ],
)
def test_domain_normalization(domain, expected):
    assert domain_url(domain) == expected


@pytest.mark.parametrize("domain", ["http://host", "https://a:b@host", "host?x=y", "host#x"])
def test_domain_rejects_credentials_and_insecure_urls(domain):
    with pytest.raises(ConfigError):
        domain_url(domain)


def test_installer_generates_credentials_once(tmp_path):
    path = tmp_path / "weewx.conf"
    initialize(path, str(tmp_path / "state"))
    original = path.read_bytes()
    cfg = ConfigObj(str(path))
    assert str(uuid.UUID(cfg["Ingest"]["collector_id"])) == cfg["Ingest"]["collector_id"]
    assert len(bytes.fromhex(cfg["Ingest"]["token"])) == 32
    assert not cfg["Ingest"]["url"]
    initialize(path, str(tmp_path / "other"))
    assert path.read_bytes() == original


def test_scan_reports_devices_and_does_not_guess_serial_hardware(monkeypatch):
    import serial.tools.list_ports
    import usb.core

    monkeypatch.setattr(
        serial.tools.list_ports,
        "comports",
        lambda **_: [SimpleNamespace(device="/dev/ttyUSB0", description="USB serial adapter")],
    )
    monkeypatch.setattr(
        usb.core,
        "find",
        lambda **_: [SimpleNamespace(idVendor=0x1941, idProduct=0x8021, bus=1, address=2)],
    )
    devices, warnings = scan()
    assert not warnings
    assert devices[0]["driver"] is None
    assert devices[1]["driver"] == "FineOffsetUSB"
    assert ("Vantage", "weewx.drivers.vantage") in drivers()


def test_guided_setup_tests_simulator_uploads_and_preserves_token(
    tmp_path, tls_server, monkeypatch
):
    path = tmp_path / "weewx.conf"
    initialize(path, str(tmp_path / "state"))
    raw = ConfigObj(str(path))
    raw["Ingest"]["ca_file"] = str(tls_server["ca"])
    raw["Simulator"]["loop_interval"] = "0.1"
    raw.write()
    secret = raw["Ingest"]["token"]
    collector = raw["Ingest"]["collector_id"]
    monkeypatch.setattr("weewx_php_ingest.configure.scan", lambda: ([], []))
    for _ in range(2):
        replies = iter([tls_server["url"], "Simulator", "n", "n"])
        monkeypatch.setattr("builtins.input", lambda replies=replies: next(replies))
        setup(path)
        cfg = load_config(path)
        assert read_token(cfg) == secret
        assert cfg.collector_id == collector
    assert len(tls_server["receipts"]) == 1
    assert len({p["station_id"] for p in tls_server["receipts"].values()}) == 1
    spools = open_spools(cfg)
    assert spools[0].status()["events"] == 1
    spools[0].close()


def test_bounded_hardware_probe_without_spool_or_upload(make_config):
    cfg = make_config(count=1)
    event = probe_packet(cfg.path, "s0", timeout=10)
    assert "outTemp" in event["data"]
    assert not cfg.state_dir.exists()
    cfg.path.write_text(cfg.path.read_text().replace("loop_interval=0.1", "loop_interval=600"))
    with pytest.raises(ConfigError, match="timed out"):
        probe_packet(cfg.path, "s0", timeout=0.3)


def test_failed_setup_preserves_config_and_station_identity(make_config, monkeypatch):
    cfg = make_config(count=1)
    spools = open_spools(cfg)
    identity = spools[0].station_id
    spools[0].close()
    original = cfg.path.read_bytes()
    monkeypatch.setattr("weewx_php_ingest.configure.select_hardware", lambda raw: raw)

    def failure(*_args, **_kwargs):
        raise ConfigError("Hardware test timed out")

    monkeypatch.setattr("weewx_php_ingest.configure.probe_packet", failure)
    replies = iter(["weather.example", "n"])
    monkeypatch.setattr("builtins.input", lambda: next(replies))
    with pytest.raises(ConfigError, match="cancelled"):
        setup(cfg.path)
    assert cfg.path.read_bytes() == original
    spools = open_spools(cfg)
    assert spools[0].station_id == identity
    spools[0].close()


def test_single_station_spool_byte_limit(tmp_path):
    from test_weewx_config import conf_data

    data = conf_data(tmp_path)
    data["Ingest"]["spool_max_bytes"] = "1048576"
    path = tmp_path / "weewx.conf"
    write_config(path, data)
    cfg = load_config(path)
    assert cfg.stations[0].max_bytes == 1048576
    assert cfg.max_bytes == 262144
