import dataclasses

import pytest

from weewx_php_ingest.config import ConfigError, endpoint_url, load_config, read_token


@pytest.mark.parametrize(
    "url",
    [
        "http://host/ingest/weewx.php",
        "https://user:secret@host/ingest/weewx.php",
        "https://host/ingest/weewx.php?token=x",
        "https://host/ingest/weewx.php?",
        "https://host/ingest/weewx.php#x",
        "https://host/ingest/weewx.php\r\nHost: evil",
        "https://host:bad/ingest/weewx.php",
        "https://host/wrong.php",
        "https://host\\evil/ingest/weewx.php",
    ],
)
def test_unsafe_endpoint_is_rejected(url):
    with pytest.raises(ConfigError):
        endpoint_url(url)


def test_token_rotation_and_injection_rejection(make_config):
    cfg = make_config(count=1)
    assert read_token(cfg) == "a" * 64
    cfg.token_file.write_text("b" * 64)
    assert read_token(cfg) == "b" * 64
    cfg.token_file.write_text("b" * 64 + "\r\nInjected: secret")
    with pytest.raises(ConfigError, match="invalid collector token"):
        read_token(cfg)


def test_token_env_is_explicit_and_config_typo_fails(make_config, monkeypatch):
    cfg = make_config(count=1)
    monkeypatch.setenv("TEST_COLLECTOR_TOKEN", "c" * 64)
    assert (
        read_token(dataclasses.replace(cfg, token_file=None, token_env="TEST_COLLECTOR_TOKEN"))
        == "c" * 64
    )
    cfg.path.write_text(cfg.path.read_text().replace("send_interval", "send_intervall"))
    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(cfg.path)


@pytest.mark.parametrize("key", ["collector", "uploader"])
def test_reserved_process_names_cannot_be_station_keys(make_config, key):
    cfg = make_config(count=1)
    cfg.path.write_text(cfg.path.read_text().replace("[[s0]]", f"[[{key}]]"))
    with pytest.raises(ConfigError, match="invalid station key"):
        load_config(cfg.path)
