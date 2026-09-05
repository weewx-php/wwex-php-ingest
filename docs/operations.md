# Deployment and operation

## Linux service

Install this project under `/opt/weewx-php-ingest` with its virtual environment
at `/opt/weewx-php-ingest/.venv`. Create the service account and directories:

```sh
sudo useradd --system --home /var/lib/weewx-php-ingest --shell /usr/sbin/nologin weewx-ingest
sudo install -d -m 0700 -o weewx-ingest -g weewx-ingest /var/lib/weewx-php-ingest
sudo install -d -m 0750 -o root -g weewx-ingest /etc/weewx-php-ingest
sudo install -m 0644 deploy/weewx-php-ingest.service /etc/systemd/system/
```

Place the configured TOML, WeeWX `.conf` files and token in `/etc/weewx-php-ingest`.
Set `state_dir = "/var/lib/weewx-php-ingest"`. The token must belong to the service
account with mode `0600`; protect driver configurations containing credentials
in the same way. Set `WEEWX_ROOT` to each station's own writable data directory
if its extension needs files. The supplied unit grants write access only beneath
`/var/lib/weewx-php-ingest`.

```sh
sudo -u weewx-ingest /opt/weewx-php-ingest/.venv/bin/weewx-php-ingest --config /etc/weewx-php-ingest/collector.toml check
sudo systemctl daemon-reload
sudo systemctl enable --now weewx-php-ingest
sudo journalctl -u weewx-php-ingest -f
```

The service includes `dialout` for serial devices. Native USB devices may need
driver-specific udev rules/groups. Use stable `/dev/serial/by-id/...` paths where
supported. Do not run another WeeWX daemon against the same console.

The supervisor uses one worker per station and one uploader process. systemd's
`KillMode=control-group` also cleans up children if the supervisor is killed.
Direct CLI runs should be stopped with Ctrl+C/SIGTERM and allowed to finish.
Configuration changes require a restart. Token-file rotation takes effect on the
next request, subject to existing authentication backoff.

## Driver configuration and extensions

Keep normal driver sections and `loader(config_dict, engine)` contracts. Two Davis
instances each have `[Vantage]` in separate files. Use the same module twice with
separate device options and distinct station keys.

Install extensions with upstream `weectl extension install` using the relevant
station's WeeWX config/environment. `WEEWX_ROOT`/`USER_ROOT` and `user.extensions`
are initialized in that station's worker. Additional import directories use
`python_paths = ["/opt/station-extension/bin"]`. Set an absolute `python` executable
per station for separate virtual environments; install the collector and the
extension's requirements in each environment. Local extensions are trusted code.

The collector replaces every `[Engine][[Services]]` group at runtime without
rewriting the configuration. No upstream calibration, conversion, QC, derived
observations, archive storage, REST uploads or report services run by default.
Driver-internal callbacks, such as Vantage gust tracking, still run. Explicitly
enable required auxiliary services in TOML:

```toml
[stations.garden.services]
prep_services = ["weewx.engine.StdTimeSynch"]
data_services = []
process_services = ["user.myservice.RequiredService"]
xtype_services = []
```

`StdTimeSynch` may set the device clock. Document corrections applied by auxiliary
services to avoid applying them again in PHP. Archive, reporting and external
upload service groups cannot be enabled in the collector profile.

`lifecycle_interval` and `lifecycle_delay` supply END_ARCHIVE_PERIOD, POST_LOOP and
PRE_LOOP coordination without reading hardware archives. This preserves periodic
gust resets and allows clock/device work between LOOP sessions. Match the period
to the device's expected interval where necessary; it does not set the PHP archive
interval or change the hardware logger.

Only numeric/null observations are accepted. A driver emitting string metadata
must explicitly exclude the relevant keys, for example `exclude_fields = ["source"]`.
Invalid timestamps, units, fields or values stop that worker with `invalid_packet`.
Correct its configuration/driver and restart the service.

## Storage, retry and recovery

`max_events` and `max_bytes` apply per station and include quarantined events.
Bytes count serialized event payloads; provision additional capacity for SQLite
indexes, free pages and WAL. `min_free_bytes` reserves disk headroom. Queued
readings are never silently expired or compacted.

At 2.5-second cadence, one station creates 34,560 events/day. A 250,000-event quota
holds about 7.2 days before the byte limit. Size both bounds using real observation
sizes and the desired outage period. A full spool pauses that worker; it holds
the current uncommitted reading and waits for room. Subsequent device observations
cannot be collected during that pause. Durability applies after SQLite commit;
an uncommitted reading cannot survive power loss. SD-card/filesystem durability
still depends on the underlying hardware.

| State | Action |
|---|---|
| `pending` | Adopt the station in PHP; station-wide retries wait at least 60 seconds. |
| `http_401`, `http_403` | Correct token, ID or receiver configuration; retry waits at least 5 minutes. |
| `http_429` | Honor Retry-After and exponential backoff. |
| `http_503`, `https_failed` | Restore availability/capacity/TLS; events remain stored. |
| `too_old`, `event_conflict`, `future_timestamp`, `station_blocked` | Retained in quarantine. Correct the cause and use `retry <station>`. |
| `spool_full` | Increase capacity or resolve delivery; collection resumes when room is available. |
| `spool_storage_failed` | Check disk/filesystem; the worker exits and the supervisor retries. |
| `silence_timeout`, worker exit | Only the affected process is restarted with backoff. |

`max_age_seconds` initializes the age warning in status; valid receiver limits
replace it after delivery. Old events are still submitted so an already committed
event with a lost ACK can receive `duplicate` within the receipt horizon. Only the
server knows whether it previously accepted an event. Unaccepted expired events
receive `too_old` and stay quarantined. No CLI command rewrites timestamps or
deletes refused events.

The uploader alternates oldest/newest readings and rotates stations, including
one-event batches. Maximum throughput is `max_packets / send_interval`
observations/second, further constrained by bytes, network time and server limits.
Configure enough throughput to drain a backlog while new readings arrive.
Pending stations do not block admitted stations. Learned lower request limits
persist across restarts.

Stop the collector before copying its complete state directory. An online copy
of only the main SQLite file can omit committed WAL data. Preserve configuration,
station keys, collector ID and token when moving to another computer.

HTTPS uses the platform trust store or `ca_file`; verification cannot be disabled.
Redirects and environment HTTP proxies are not followed. Use the final
`/ingest/weewx.php` URL, optionally under a subdirectory.

## Tests and builds

```sh
python -m pip install -e '.[test]'
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
python -m pip_audit --skip-editable
python -m build
```

The optional PHP integration test needs the separate project, PHP and SQLite3:

```sh
WEEWX_PHP_ROOT=/path/to/weewx-php python -m pytest -q tests/test_php_integration.py
```

It starts isolated local HTTP/TLS test servers and creates a temporary receiver
configuration/database. It does not modify PHP project files or use production
credentials. Without `WEEWX_PHP_ROOT`, that one test is skipped.
