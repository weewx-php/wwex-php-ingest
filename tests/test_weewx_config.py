import stat
import time
import uuid

import pytest

from weewx_php_ingest.config import ConfigError, load_config, read_token
from weewx_php_ingest.configure import write_config
from weewx_php_ingest.runtime import driver_config
from weewx_php_ingest.supervisor import Supervisor


def conf_data(tmp_path):
    return {
        "WEEWX_ROOT": str(tmp_path),
        "Station": {
            "station_type": "Simulator",
            "latitude": "0",
            "longitude": "0",
            "altitude": ["0", "meter"],
        },
        "Simulator": {"driver": "weewx.drivers.simulator", "loop_interval": "0.1"},
        "Ingest": {
            "collector_id": str(uuid.uuid4()),
            "url": "https://localhost/ingest/weewx.php",
            "token": "a" * 64,
            "state_dir": str(tmp_path / "state"),
            "station_key": "garden",
            "silence_timeout": "10",
            "startup_timeout": "10",
            "min_free_bytes": "0",
        },
    }


def test_standard_weewx_config_with_inline_credentials(tmp_path):
    path = tmp_path / "weewx.conf"
    write_config(path, conf_data(tmp_path))
    cfg = load_config(path)
    assert cfg.stations[0].key == "garden"
    assert read_token(cfg) == "a" * 64
    assert "a" * 64 not in repr(cfg)
    raw, module = driver_config(cfg.stations[0])
    assert module == "weewx.drivers.simulator"
    assert "Ingest" not in raw and "Stations" not in raw
    content = path.read_text().replace("a" * 64, "b" * 64)
    path.write_text(content)
    assert read_token(cfg) == "b" * 64
    with pytest.raises(ConfigError, match="already exists"):
        write_config(path, conf_data(tmp_path))


def test_nested_same_driver_instances_have_separate_configs(tmp_path):
    data = conf_data(tmp_path)
    station = {k: v for k, v in data.items() if k in ("Station", "Simulator")}
    data["Stations"] = {
        "garden": station,
        "roof": {
            **station,
            "Simulator": {"driver": "weewx.drivers.simulator", "loop_interval": "0.2"},
        },
    }
    for key in ("Station", "Simulator"):
        del data[key]
    for key in ("station_key", "silence_timeout", "startup_timeout", "min_free_bytes"):
        del data["Ingest"][key]
    path = tmp_path / "weewx.conf"
    write_config(path, data)
    cfg = load_config(path)
    a, _ = driver_config(cfg.stations[0])
    b, _ = driver_config(cfg.stations[1])
    assert a["Simulator"]["loop_interval"] == "0.1"
    assert b["Simulator"]["loop_interval"] == "0.2"
    assert "Stations" not in a and "Ingest" not in b


@pytest.mark.parametrize(
    "key,value",
    [
        ("send_interval", "nan"),
        ("max_packets", "1.5"),
        ("send_intervall", "10"),
        ("token", ["a" * 64, "b" * 64]),
        ("token_env", "ALSO_SET"),
        ("url", "http://host/ingest/weewx.php"),
    ],
)
def test_invalid_ini_settings_fail_without_exposing_token(tmp_path, key, value):
    data = conf_data(tmp_path)
    data["Ingest"][key] = value
    with pytest.raises(ConfigError) as error:
        write_config(tmp_path / "weewx.conf", data)
    assert "a" * 64 not in str(error.value)


def test_real_worker_with_unified_configuration(tmp_path):
    path = tmp_path / "weewx.conf"
    write_config(path, conf_data(tmp_path))
    supervisor = Supervisor(load_config(path))
    supervisor.children = supervisor.children[:1]
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            supervisor.poll()
            if supervisor.spools[0].status()["events"] >= 2:
                break
            time.sleep(0.05)
        assert supervisor.spools[0].status()["events"] >= 2
    finally:
        supervisor.shutdown()


def test_credential_permissions_on_posix(tmp_path):
    import os

    if os.name != "posix":
        pytest.skip("POSIX file mode")
    path = tmp_path / "weewx.conf"
    write_config(path, conf_data(tmp_path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    with pytest.raises(ConfigError, match="0600"):
        read_token(load_config(path))
