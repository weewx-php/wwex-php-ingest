"""Unified WeeWX driver and ingest configuration."""

import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from configobj import ConfigObj, ConfigObjError

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
    section: str | None = None


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
    inline_token: bool = False


def read_weewx(path):
    try:
        return ConfigObj(str(path), encoding="utf-8", interpolation=False, file_error=True)
    except (OSError, ConfigObjError, UnicodeError) as exc:
        raise ConfigError("cannot read weewx.conf") from exc


def _ini_options(section, allowed, mapping=None):
    mapping = mapping or {}
    keys(section, allowed, "Ingest")
    integer_keys = {
        "max_packets",
        "max_bytes",
        "spool_max_bytes",
        "max_events",
        "min_free_bytes",
        "max_age_seconds",
        "lifecycle_interval",
        "lifecycle_delay",
    }
    float_keys = {
        "send_interval",
        "timeout",
        "backoff_max",
        "shutdown_timeout",
        "silence_timeout",
        "startup_timeout",
    }
    result = {}
    for key, value in section.items():
        key = mapping.get(key, key)
        try:
            if key in integer_keys:
                value = int(value)
            elif key in float_keys:
                value = float(value)
            elif key in ("python_paths", "exclude_fields"):
                value = value if isinstance(value, list) else [value] if value else []
            elif key == "services":
                value = {
                    k: v if isinstance(v, list) else [v] if v else [] for k, v in value.items()
                }
        except (ValueError, TypeError, AttributeError) as exc:
            raise ConfigError(f"invalid {key}") from exc
        result[key] = value
    return result


def _from_weewx(path):
    cfg = read_weewx(path)
    raw_ingest = cfg.get("Ingest", {})
    collector = _ini_options(
        raw_ingest,
        "collector_id url token token_file token_env token_header state_dir ca_file "
        "send_interval timeout backoff_max max_packets max_bytes max_age_seconds shutdown_timeout "
        "station_key label silence_timeout startup_timeout max_events "
        "spool_max_bytes min_free_bytes "
        "lifecycle_interval lifecycle_delay exclude_fields python python_paths Services",
        {"collector_id": "id", "url": "endpoint", "Services": "services"},
    )
    station_settings = (
        "label silence_timeout startup_timeout max_events min_free_bytes "
        "lifecycle_interval lifecycle_delay exclude_fields python python_paths services"
    )
    stations = {}
    if "Stations" in cfg:
        if not isinstance(cfg["Stations"], dict):
            raise ConfigError("invalid Stations section")
        if "Station" in cfg or "station_key" in collector:
            raise ConfigError("use Station or Stations, not both")
        for key, section in cfg["Stations"].items():
            if not isinstance(section, dict):
                raise ConfigError("invalid station section")
            options = _ini_options(
                section.get("Ingest", {}),
                station_settings.replace("services", "Services") + " max_bytes",
                {"Services": "services"},
            )
            stations[key] = {"config": str(path), "section": key, **options}
        if any(k in collector for k in station_settings.split()) or "spool_max_bytes" in collector:
            raise ConfigError("put per-station settings inside Stations")
    else:
        key = collector.pop("station_key", "station")
        if not isinstance(key, str):
            raise ConfigError("invalid station key")
        options = {k: collector.pop(k) for k in station_settings.split() if k in collector}
        if "spool_max_bytes" in collector:
            options["max_bytes"] = collector.pop("spool_max_bytes")
        stations[key] = {"config": str(path), **options}
    # Only a flag travels in Config. The token is reread for each request, never kept in repr.
    if "token" in collector:
        collector.pop("token")
        collector["inline_token"] = True
    return {"collector": collector, "stations": stations}


def load_config(path):
    path = Path(path).resolve()
    try:
        raw = _from_weewx(path)
    except ConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read collector configuration") from exc
    keys(raw, "collector stations", "root")
    c = raw.get("collector", {})
    keys(
        c,
        "id endpoint state_dir token_file token_env token_header ca_file send_interval timeout "
        "backoff_max max_packets max_bytes max_age_seconds shutdown_timeout inline_token",
        "collector",
    )
    if not valid_uuid(c.get("id")):
        raise ConfigError("Ingest.collector_id must be a UUID")
    endpoint = endpoint_url(c.get("endpoint"))

    def relative(value):
        if not isinstance(value, str) or not value:
            raise ConfigError("invalid file path")
        return (path.parent / value).resolve()

    token_file, token_env = c.get("token_file"), c.get("token_env")
    inline_token = c.get("inline_token", False)
    if type(inline_token) is not bool or sum(map(bool, (token_file, token_env, inline_token))) != 1:
        raise ConfigError("set exactly one of token, token_file or token_env")
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
            "max_bytes min_free_bytes lifecycle_interval lifecycle_delay exclude_fields "
            "services section",
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
                section=s.get("section"),
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
        inline_token,
    )


def read_token(config):
    try:
        secret_path = config.path if config.inline_token else config.token_file
        if secret_path and os.name != "nt" and stat.S_IMODE(secret_path.stat().st_mode) & 0o077:
            raise ConfigError("credential file permissions must be 0600 or stricter")
        if config.inline_token:
            token = read_weewx(config.path).get("Ingest", {}).get("token", "")
        elif config.token_file:
            with config.token_file.open("r", encoding="ascii") as stream:
                token = stream.read(4097).strip()
        else:
            token = os.environ.get(config.token_env, "")
    except (OSError, UnicodeError) as exc:
        raise ConfigError("cannot read collector token") from exc
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ConfigError("invalid collector token")
    return token
