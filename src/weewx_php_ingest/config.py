"""Local administrator configuration; credentials are read only by the uploader."""

import os
import re
import stat
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .protocol import valid_uuid


class ConfigError(ValueError):
    pass


def number(table, key, default, minimum, maximum):
    value = table.get(key, default)
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ConfigError(f"invalid {key}")
    return value


def integer(table, key, default, minimum, maximum):
    value = number(table, key, default, minimum, maximum)
    if type(value) is not int:
        raise ConfigError(f"invalid {key}")
    return value


def keys(table, allowed, section):
    if not isinstance(table, dict) or set(table) - set(allowed.split()):
        raise ConfigError(f"unknown setting in {section}")


def endpoint_url(value):
    if (
        not isinstance(value, str)
        or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
        or "?" in value
        or "#" in value
        or "\\" in value
    ):
        raise ConfigError("endpoint must be an HTTPS URL without query or fragment")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.endswith("/ingest/weewx.php")
        ):
            raise ValueError
        _ = parsed.port
    except ValueError as exc:
        raise ConfigError("invalid HTTPS ingest endpoint") from exc
    return value


@dataclass(frozen=True)
class StationConfig:
    key: str
    config: Path
    label: str
    python: str = sys.executable
    python_paths: tuple[Path, ...] = ()
    silence_timeout: float = 120
    startup_timeout: float = 180
    max_events: int = 250000
    max_bytes: int = 268435456
    min_free_bytes: int = 67108864
    lifecycle_interval: int = 300
    lifecycle_delay: int = 15
    exclude_fields: tuple[str, ...] = ()
    services: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    path: Path
    collector_id: str
    endpoint: str
    state_dir: Path
    token_file: Path | None
    token_env: str | None
    token_header: str
    ca_file: Path | None
    send_interval: float
    timeout: float
    backoff_max: float
    max_packets: int
    max_bytes: int
    max_age_seconds: int
    shutdown_timeout: float
    stations: tuple[StationConfig, ...]


def load_config(path):
    path = Path(path).resolve()
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read collector configuration") from exc
    keys(raw, "collector stations", "root")
    c = raw.get("collector", {})
    keys(
        c,
        "id endpoint state_dir token_file token_env token_header ca_file send_interval timeout "
        "backoff_max max_packets max_bytes max_age_seconds shutdown_timeout",
        "collector",
    )
    if not valid_uuid(c.get("id")):
        raise ConfigError("collector.id must be the provisioned UUID")
    endpoint = endpoint_url(c.get("endpoint"))

    def relative(value):
        if not isinstance(value, str) or not value:
            raise ConfigError("invalid file path")
        return (path.parent / value).resolve()

    token_file, token_env = c.get("token_file"), c.get("token_env")
    if bool(token_file) == bool(token_env):
        raise ConfigError("set exactly one of token_file or token_env")
    if token_env and (
        not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env)
    ):
        raise ConfigError("invalid token_env")
    header = c.get("token_header", "Authorization")
    if header not in ("Authorization", "X-WeeWX-Token"):
        raise ConfigError("invalid token_header")
    stations = []
    raw_stations = raw.get("stations", {})
    if not isinstance(raw_stations, dict) or not raw_stations:
        raise ConfigError("configure at least one station")
    for key, s in raw_stations.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key) or key in ("collector", "uploader"):
            raise ConfigError("invalid station key")
        keys(
            s,
            "config label python python_paths silence_timeout startup_timeout max_events "
            "max_bytes min_free_bytes lifecycle_interval lifecycle_delay exclude_fields services",
            f"station {key}",
        )
        services = s.get("services", {})
        keys(services, "prep_services data_services process_services xtype_services", "services")
        for entries in services.values():
            if (
                not isinstance(entries, list)
                or any(not isinstance(x, str) for x in entries)
                or any(
                    x.startswith(("weewx.restx.", "weewx.reportengine."))
                    or x in ("weewx.engine.StdArchive", "weewx.engine.StdPrint")
                    for x in entries
                )
            ):
                raise ConfigError("invalid auxiliary services")
        paths, excluded = s.get("python_paths", []), s.get("exclude_fields", [])
        if (
            not isinstance(paths, list)
            or not isinstance(excluded, list)
            or any(not isinstance(x, str) for x in excluded)
            or set(excluded) & {"dateTime", "usUnits"}
        ):
            raise ConfigError("invalid python_paths or exclude_fields")
        interval = integer(s, "lifecycle_interval", 300, 2, 86400)
        delay = integer(s, "lifecycle_delay", 15, 1, 86399)
        if delay >= interval:
            raise ConfigError("lifecycle_delay must be less than lifecycle_interval")
        python = s.get("python", sys.executable)
        if not isinstance(python, str) or not Path(python).is_absolute():
            raise ConfigError("python must be an absolute executable path")
        label = s.get("label", key)
        if not isinstance(label, str) or len(label) > 160:
            raise ConfigError("invalid station label")
        stations.append(
            StationConfig(
                key=key,
                config=relative(s.get("config")),
                label=label,
                python=python,
                python_paths=tuple(relative(p) for p in paths),
                silence_timeout=number(s, "silence_timeout", 120, 1, 86400),
                startup_timeout=number(s, "startup_timeout", 180, 1, 86400),
                max_events=integer(s, "max_events", 250000, 1, 100000000),
                max_bytes=integer(s, "max_bytes", 268435456, 1024, 10**13),
                min_free_bytes=integer(s, "min_free_bytes", 67108864, 0, 10**13),
                lifecycle_interval=interval,
                lifecycle_delay=delay,
                exclude_fields=tuple(excluded),
                services=services,
            )
        )
    return Config(
        path,
        c["id"],
        endpoint,
        relative(c.get("state_dir", "state")),
        relative(token_file) if token_file else None,
        token_env,
        header,
        relative(c["ca_file"]) if c.get("ca_file") else None,
        number(c, "send_interval", 10, 0.1, 86400),
        number(c, "timeout", 20, 1, 300),
        number(c, "backoff_max", 900, 1, 86400),
        integer(c, "max_packets", 128, 1, 128),
        integer(c, "max_bytes", 262144, 1024, 262144),
        integer(c, "max_age_seconds", 432000, 1, 604800),
        number(c, "shutdown_timeout", 10, 1, 300),
        tuple(stations),
    )


def read_token(config):
    try:
        if config.token_file:
            if os.name != "nt" and stat.S_IMODE(config.token_file.stat().st_mode) & 0o077:
                raise ConfigError("token_file permissions must be 0600 or stricter")
            with config.token_file.open("r", encoding="ascii") as stream:
                token = stream.read(4097).strip()
        else:
            token = os.environ.get(config.token_env, "")
    except (OSError, UnicodeError) as exc:
        raise ConfigError("cannot read collector token") from exc
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ConfigError("invalid collector token")
    return token
