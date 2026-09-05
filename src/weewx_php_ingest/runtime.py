"""Real, process-local WeeWX engine with a collector-only service profile."""

import importlib
import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path

from configobj import ConfigObj, ConfigObjError

from .config import ConfigError, read_weewx
from .protocol import MODULE, ProtocolError, encode, event_from_archive, event_from_loop
from .sensor_sources import MODULES
from .spool import Spool, SpoolFull, spool_path

SERVICE_GROUPS = (
    "prep_services",
    "data_services",
    "process_services",
    "xtype_services",
    "archive_services",
    "restful_services",
    "report_services",
)
log = logging.getLogger(__name__)
VIRTUAL_MODULE = "weewx_php_ingest.virtual"


def driver_config(station):
    try:
        cfg = read_weewx(station.config)
        if station.section:
            roots = {key: cfg[key] for key in ("WEEWX_ROOT", "USER_ROOT") if key in cfg}
            cfg = ConfigObj(
                {**roots, **cfg["Stations"][station.section].dict()}, interpolation=False
            )
        # Never pass ingest credentials or another instance's configuration to a driver.
        cfg.pop("Ingest", None)
        cfg.pop("Stations", None)
        module = cfg[cfg["Station"]["station_type"]]["driver"]
        if not isinstance(module, str) or not MODULE.fullmatch(module) or len(module) > 160:
            raise ValueError
        for name in ("latitude", "longitude", "altitude"):
            if name not in cfg["Station"]:
                raise ValueError
    except (OSError, ConfigObjError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(f"invalid WeeWX configuration for {station.key}") from exc
    cfg["WEEWX_ROOT"] = str((station.config.parent / cfg.get("WEEWX_ROOT", ".")).resolve())
    cfg["config_path"] = str(station.config)
    cfg["log_events"] = False
    # Explicitly replace all groups: omitted xtype_services would load upstream defaults.
    cfg["Engine"] = {"Services": {group: [] for group in SERVICE_GROUPS}}
    for group, entries in station.services.items():
        cfg["Engine"]["Services"][group] = list(entries)
    if module in MODULES.values():
        importlib.import_module(module).settings(cfg[cfg["Station"]["station_type"]])
        if any(station.services.values()):
            raise ConfigError("Multi-device receivers do not accept LOOP services")
        cfg.setdefault("StdArchive", {})["record_generation"] = "software"
    if module == VIRTUAL_MODULE:
        from .virtual import options

        options(cfg[cfg["Station"]["station_type"]])
        archive = cfg.setdefault("StdArchive", {})
        if archive.get("record_generation", "software") != "software":
            raise ConfigError("Virtual stations require software record_generation")
        archive["record_generation"] = "software"
        archive.setdefault("archive_interval", str(station.lifecycle_interval))
        archive.setdefault("archive_delay", str(station.lifecycle_delay))
    archive_options(cfg, station)
    return cfg, module


def archive_options(cfg, station):
    from weeutil.weeutil import to_bool

    options = cfg.get("StdArchive", {})
    try:
        mode = options.get("record_generation", "hardware")
        if mode not in ("hardware", "software"):
            raise ValueError
        interval = int(options.get("archive_interval", station.lifecycle_interval))
        delay = int(options.get("archive_delay", station.lifecycle_delay))
        no_catchup = to_bool(options.get("no_catchup", False))
        if not 1 <= interval <= 86400 or not 1 <= delay <= 86399:
            raise ValueError
    except (AttributeError, ValueError, TypeError) as exc:
        raise ConfigError("invalid StdArchive settings") from exc
    return mode, interval, delay, no_catchup


def create_engine(cfg, station, spool, stop_requested=lambda: False, max_age_seconds=432000):
    import weewx
    import weewx.engine

    virtual = cfg[cfg["Station"]["station_type"]]["driver"] == VIRTUAL_MODULE

    class CollectorEngine(weewx.engine.StdEngine):
        def dispatchEvent(self, event):
            super().dispatchEvent(event)
            if virtual and event.event_type == weewx.NEW_LOOP_PACKET:
                if not any(
                    value is not None
                    for key, value in event.packet.items()
                    if key not in ("dateTime", "usUnits") and key not in station.exclude_fields
                ):
                    spool.set_meta("collection_state", "waiting_for_service")
                    if stop_requested():
                        raise weewx.StopNow()
                    return
            if event.event_type == weewx.NEW_LOOP_PACKET or (
                event.event_type == weewx.NEW_ARCHIVE_RECORD
                and getattr(event, "origin", None) == "hardware"
            ):
                # After ALL driver/auxiliary callbacks, even callbacks registered during STARTUP.
                archive = event.event_type == weewx.NEW_ARCHIVE_RECORD
                serialize = event_from_archive if archive else event_from_loop
                packet = serialize(
                    event.record if archive else event.packet,
                    spool.station_id,
                    spool.get_meta("driver_module"),
                    station.exclude_fields,
                )
                full_logged = False
                while True:
                    try:
                        spool.append(packet)
                        break
                    except SpoolFull:
                        if not full_logged:
                            log.error("station %s: spool_full; collection paused", station.key)
                            full_logged = True
                        with spool.transaction():
                            spool.set_meta("collection_state", "spool_full")
                            spool.set_meta("collection_error", "spool_full")
                            spool.set_meta("blocked_bytes", len(encode(packet)))
                        # Hold this exact reading while upload frees space. Do not read the device.
                        if stop_requested():
                            raise weewx.StopNow() from None
                        time.sleep(0.5)
            if stop_requested():
                raise weewx.StopNow()

    mode, interval, delay, no_catchup = archive_options(cfg, station)
    engine = CollectorEngine(cfg)
    hardware = mode == "hardware"
    if hardware:
        try:
            reported = engine.console.archive_interval
            if (
                type(reported) not in (int, float)
                or not 1 <= reported <= 86400
                or int(reported) != reported
            ):
                raise ConfigError("invalid hardware archive_interval")
            interval = int(reported)
        except NotImplementedError:
            pass  # Some drivers implement catch-up without advertising their interval.
        except BaseException:
            engine.shutDown()
            raise
    if delay >= interval:
        engine.shutDown()
        raise ConfigError("archive_delay must be less than the effective polling interval")
    spool.set_meta("record_generation", mode)
    spool.set_meta("hardware_interval", interval if hardware else None)
    spool.set_meta("hardware_state", "starting" if hardware else "disabled")
    spool.set_meta("hardware_error", None)

    def catch_up(startup=False):
        nonlocal hardware
        if not hardware:
            return
        if startup and no_catchup:
            # An explicit skip sets a durable starting point once, not on every restart.
            if spool.get_meta("hardware_cursor") is None:
                spool.set_meta("hardware_cursor", int(time.time()))
            return
        age = min(max_age_seconds, spool.get_meta("receiver_max_age_seconds") or max_age_seconds)
        since = max(spool.get_meta("hardware_cursor", 0), int(time.time()) - age)
        generator = (
            engine.console.genStartupRecords if startup else engine.console.genArchiveRecords
        )
        try:
            for record in generator(since):
                if stop_requested():
                    raise weewx.StopNow()
                # Drivers must yield chronologically, as required by WeeWX's archive API.
                timestamp = record.get("dateTime")
                if type(timestamp) is not int:
                    raise ProtocolError("invalid_timestamp")
                if timestamp <= max(since, spool.get_meta("hardware_cursor", 0)):
                    continue
                if timestamp > time.time() + 60:
                    raise ProtocolError("future_hardware_timestamp")
                engine.dispatchEvent(
                    weewx.Event(weewx.NEW_ARCHIVE_RECORD, record=record, origin="hardware")
                )
            spool.set_meta("hardware_state", "active")
            spool.set_meta("hardware_error", None)
        except NotImplementedError:
            # Unsupported hardware falls back to LOOP -> PHP software accumulation.
            # A driver may implement genArchiveRecords but disable startup catch-up.
            if not startup:
                hardware = False
            spool.set_meta("hardware_state", "software_fallback")
            spool.set_meta("hardware_error", None)
        except weewx.HardwareError as exc:
            spool.set_meta("hardware_state", "retrying")
            spool.set_meta("hardware_error", type(exc).__name__)
            log.warning(
                "station %s: hardware catch-up failed (%s)", station.key, type(exc).__name__
            )

    period_end = None
    delay_end = None

    def pre_loop(_event):
        nonlocal period_end, delay_end
        if period_end is None:
            now = engine._get_console_time()
            period_end = int(now // interval + 1) * interval
            delay_end = period_end + delay

    def check_loop(event):
        nonlocal period_end
        if event.packet["dateTime"] > period_end:
            engine.dispatchEvent(
                weewx.Event(weewx.END_ARCHIVE_PERIOD, packet=event.packet, end=period_end)
            )
            period_end = (event.packet["dateTime"] // interval + 1) * interval
        if event.packet["dateTime"] >= delay_end:
            raise weewx.engine.BreakLoop()

    def post_loop(_event):
        nonlocal delay_end
        catch_up()
        delay_end = period_end + delay

    engine.bind(weewx.STARTUP, lambda _event: catch_up(startup=True))
    engine.bind(weewx.PRE_LOOP, pre_loop)
    engine.bind(weewx.CHECK_LOOP, check_loop)
    engine.bind(weewx.POST_LOOP, post_loop)
    return engine


def run_worker(config, station, stop_file):
    for path in reversed(station.python_paths):
        sys.path.insert(0, str(path))
    import weeutil.startup
    import weewx

    cfg, module = driver_config(station)
    spool = Spool(spool_path(config, station), config.collector_id, module, station)
    stop_file = Path(stop_file)

    def stop(_signum, _frame):
        raise weewx.StopNow()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        spool.set_meta("collection_state", "starting")
        if module in MODULES.values():
            importlib.import_module(module).run(
                config, station, spool, cfg[cfg["Station"]["station_type"]], stop_file.exists
            )
            return 0
        weeutil.startup.initialize(cfg)
        engine = create_engine(cfg, station, spool, stop_file.exists, config.max_age_seconds)
        engine.run()
    except weewx.StopNow:
        spool.set_meta("collection_state", "stopped")
        return 0
    except ProtocolError as exc:
        spool.set_meta("collection_state", "invalid_packet")
        spool.set_meta("collection_error", str(exc))
        log.error("station %s: %s", station.key, exc)
        return 65
    except sqlite3.Error:
        log.error("station %s: spool_storage_failed", station.key)
        return 74
    except Exception as exc:
        spool.set_meta("collection_state", "driver_failed")
        spool.set_meta("collection_error", type(exc).__name__)
        log.error("station %s: driver_failed (%s)", station.key, type(exc).__name__)
        return 70
    finally:
        spool.close()
