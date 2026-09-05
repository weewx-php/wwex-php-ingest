"""Real, process-local WeeWX engine with a collector-only service profile."""

import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path

from configobj import ConfigObj, ConfigObjError

from .config import ConfigError, read_weewx
from .protocol import MODULE, ProtocolError, encode, event_from_loop
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
    return cfg, module


def create_engine(cfg, station, spool, stop_requested=lambda: False):
    import weewx
    import weewx.engine

    class CollectorEngine(weewx.engine.StdEngine):
        def dispatchEvent(self, event):
            super().dispatchEvent(event)
            if event.event_type == weewx.NEW_LOOP_PACKET:
                # After ALL driver/auxiliary callbacks, even callbacks registered during STARTUP.
                packet = event_from_loop(
                    event.packet,
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

    engine = CollectorEngine(cfg)
    interval, delay = station.lifecycle_interval, station.lifecycle_delay
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
        delay_end = period_end + delay

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
        weeutil.startup.initialize(cfg)
        engine = create_engine(cfg, station, spool, stop_file.exists)
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
