"""Collector CLI. No secrets accepted as command-line arguments."""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time

from .config import ConfigError, load_config, read_token
from .locking import AlreadyRunning, ProcessLock
from .runtime import driver_config, run_worker
from .spool import SpoolError
from .supervisor import Supervisor, open_spools
from .transport import HTTPSClient
from .uploader import Uploader


def main(argv=None):
    parser = argparse.ArgumentParser(prog="weewx-php-ingest")
    parser.add_argument("--config", default="/etc/weewx-php-ingest/weewx.conf")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "init", "status", "run", "upload-once", "_upload", "update"):
        commands.add_parser(command)
    for command in ("configure", "_initialize"):
        commands.add_parser(command).add_argument(
            "--state-dir", default="/var/lib/weewx-php-ingest"
        )
    probe = commands.add_parser("_probe")
    probe.add_argument("station")
    probe.add_argument("destination")
    commands.add_parser("scan")
    commands.add_parser("_worker").add_argument("station")
    commands.add_parser("retry").add_argument("station")
    args = parser.parse_args(argv)
    if os.name != "nt":
        os.umask(0o077)
    handler = logging.StreamHandler()
    handler.addFilter(lambda record: record.name.startswith("weewx_php_ingest"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler],
        force=True,
    )
    try:
        if args.command == "update":
            from .update import update

            return update()
        if args.command == "scan":
            from .hardware import scan

            devices, warnings = scan()
            print(json.dumps({"devices": devices, "warnings": warnings}, indent=2))
            return 0
        if args.command == "_probe":
            from .hardware import run_probe

            return run_probe(args.config, args.station, args.destination)
        if args.command == "_initialize":
            from .configure import initialize

            initialize(args.config, args.state_dir)
            return 0
        if args.command == "configure":
            from .configure import configure

            configure(args.config, args.state_dir)
            return 0
        config = load_config(args.config)
        if args.command == "check":
            for station in config.stations:
                driver_config(station)
            read_token(config)
            HTTPSClient(config)
            print("Configuration OK")
            return 0
        if args.command == "run":
            read_token(config)
            HTTPSClient(config)
            with ProcessLock(config.state_dir / "collector.lock"):
                Supervisor(config).run()
            return 0
        if args.command == "_worker":
            station = next((s for s in config.stations if s.key == args.station), None)
            if station is None:
                raise ConfigError("unknown station")
            with ProcessLock(config.state_dir / f"{station.key}.lock"):
                return run_worker(config, station, config.state_dir / "stop")
        spools = open_spools(config)
        try:
            if args.command in ("init", "status"):
                print(
                    json.dumps(
                        {
                            "collector_id": config.collector_id,
                            "stations": [s.status(config.max_age_seconds) for s in spools],
                        },
                        indent=2,
                    )
                )
            elif args.command == "retry":
                spool = next((s for s in spools if s.station.key == args.station), None)
                if spool is None:
                    raise ConfigError("unknown station")
                print(json.dumps({"requeued": spool.retry_quarantined()}))
            else:
                with ProcessLock(config.state_dir / "uploader.lock"):
                    uploader = Uploader(config, spools, HTTPSClient(config))
                    if args.command == "upload-once":
                        count = uploader.tick()
                        print(json.dumps({"confirmed": count}))
                    else:
                        while not (config.state_dir / "stop").exists():
                            spools[0].set_meta("uploader_heartbeat", time.time())
                            uploader.tick()
                            time.sleep(0.25)
        finally:
            for spool in spools:
                spool.close()
        return 0
    except (ConfigError, SpoolError, AlreadyRunning) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("storage_or_process_error", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
