# Deployment and operation

## Installation and service

Install directly; guided setup starts afterward:

```sh
curl -fsSL https://raw.githubusercontent.com/weewx-php/wwex-php-ingest/main/install.sh | sudo bash
```

Alternatively, install from a repository checkout: `sudo bash install.sh --source "$PWD"`.
The installer manages these paths:

| Path | Contents |
|---|---|
| `/opt/weewx-php-ingest/releases/` | Collector and bundled WeeWX source, virtual environment per version |
| `/opt/weewx-php-ingest/current` | Active release |
| `/etc/weewx-php-ingest/weewx.conf` | Driver settings, ingest URL and token; mode 0600 |
| `/var/lib/weewx-php-ingest/` | Persistent station IDs, queues and extension data |
| `/usr/local/bin/weewx-php-ingest` | CLI |

`sudo weewx-php-ingest update` builds the latest `main` in a new release directory,
verifies all bundled driver files and checks configuration before stopping the
service. Activation switches a symlink. If service startup fails, the previous
release and service unit are restored. Existing configuration and queue contents
are preserved. The repository is public; installation and updates over HTTPS
do not require a GitHub login.

### Unattended installation

Use `--non-interactive` to skip setup:

```sh
curl -fsSL https://raw.githubusercontent.com/weewx-php/wwex-php-ingest/main/install.sh | sudo bash -s -- --non-interactive
```

On a new installation, the service remains stopped until setup is complete.

### Setup and diagnostics

For unattended installation, use `--non-interactive`. If no configuration exists,
the installer generates the token and collector ID and leaves the service stopped.
Run the guided assistant to set the domain, scan/select devices and test hardware:

```sh
sudo weewx-php-ingest configure
sudo journalctl -u weewx-php-ingest -f
```

The service account joins available `dialout` and `plugdev` groups. Native USB
hardware may also need driver-specific udev rules. Prefer stable
`/dev/serial/by-id/...` device paths. Only one process may use a console.
The service can write beneath `/var/lib/weewx-php-ingest`; extension data belongs
there. The assistant stops and restarts the service around hardware setup. Manual
configuration edits require a restart. Token changes are read on the next
request, subject to existing authentication backoff.

The local token is sent over HTTPS on the first upload. The PHP receiver keeps
only its SHA-256 digest and discovers stations as pending. Use the existing Adopt
button or `php bin/weewx-php ingest adopt <sender>`. Pending readings stay in the
collector queue. No receiver-side registration command is needed.

`weewx-php-ingest scan` lists USB/serial devices. USB IDs suggest matching drivers;
generic serial adapters cannot identify a weather station model. The assistant
asks for network device addresses directly. Hardware tests wait up to 60 seconds
for an actual LOOP reading in a separate process. Failed tests return to setup;
no configuration is saved before all selected stations pass.

## Driver configuration and extensions

One station uses ordinary `[Station]` and `[Vantage]`, `[Simulator]` or another
WeeWX driver section. `[Ingest]` holds URL, token, collector ID and collector
settings. File paths are relative to `weewx.conf`; use absolute paths for a service.
Exactly one of `token`, `token_file` and `token_env` may supply the token.
Credentials never belong in the URL or command-line arguments.

Multiple stations use `[Stations]`, `[[garden]]`, then `[[[Station]]]` and the
normal driver section as `[[[Vantage]]]`. Each instance receives its own configuration
and engine. See [weewx-multi.conf](../examples/weewx-multi.conf).
Keep station keys and the state directory stable to preserve identities.

The repository bundles the official WeeWX drivers and matching engine. The daily
GitHub Action checks stable PyPI releases, validates the archive and file checksums,
and tests the changed release before committing. It can also be run manually from
GitHub Actions. This does not automatically restart or update installed stations.

Keep third-party `user.*` modules in `/var/lib/weewx-php-ingest/bin/user/`, with
`WEEWX_ROOT = /var/lib/weewx-php-ingest` and `USER_ROOT = bin/user`. Set separate
roots inside each station section if an extension stores instance-specific data.
Put additional pip requirements in `/etc/weewx-php-ingest/requirements.txt`, owned
by root. The installer reapplies them when building each new version. Extension
files and configuration remain outside release directories. Use each extension's
upstream instructions for its additional files and driver options. Local drivers,
services, requirements and import paths are trusted administrator code.

### Installing drivers from GitHub

The bundled `weectl` CLI is located at
`/opt/weewx-php-ingest/current/.venv/bin/weectl`. It installs
[WeeWX extensions](https://weewx.com/docs/latest/custom/extensions/)
from ZIP/tarball URLs, local archives or directories. Use the download URL from
the driver's instructions; a GitHub repository page is not sufficient.
The package must include a WeeWX installer (`install.py`).

1. Stop the service and open the configuration:

   ```sh
   sudo systemctl stop weewx-php-ingest
   sudoedit /etc/weewx-php-ingest/weewx.conf
   ```

2. Add the following top-level values if missing.
   Place `WEEWX_ROOT` and `USER_ROOT` before the first section.
   Extend existing sections instead of duplicating them. WeeWX requires
   `StdReport` when merging extension options; this does not enable reports
   in the collector.

   ```ini
   WEEWX_ROOT = /var/lib/weewx-php-ingest
   USER_ROOT = bin/user

   [StdReport]
       HTML_ROOT = public_html
       SKIN_ROOT = skins

   [Engine]
       [[Services]]
   ```

3. Install the package. Replace `OWNER`, `REPO` and `VERSION` with the driver's
   details. This command protects the newly written token configuration and
   makes installed Python files readable by the service:

   ```sh
   sudo sh -c '
   set -eu
   umask 077
   /opt/weewx-php-ingest/current/.venv/bin/weectl extension install "$1" \
     --config=/etc/weewx-php-ingest/weewx.conf
   chmod 600 /etc/weewx-php-ingest/weewx.conf
   chown weewx-ingest:weewx-ingest /etc/weewx-php-ingest/weewx.conf
   chmod -R go+rX /var/lib/weewx-php-ingest/bin
   ' sh 'https://github.com/OWNER/REPO/archive/refs/tags/VERSION.zip'
   ```

4. Add the driver section to `weewx.conf` according to its instructions and set
   the matching `station_type` under `[Station]`. Example with placeholders:

   ```ini
   [Station]
       station_type = DRIVER_NAME
       # Keep the existing latitude, longitude and altitude.

   [DRIVER_NAME]
       driver = user.DRIVER_MODULE
       # Connection options from the driver's instructions.
   ```

   For multiple stations, place these settings in the corresponding `[Stations]`
   subsection. Driver options added by `weectl` initially appear at the top
   level and must be moved into that subsection. The guided assistant currently
   offers only the bundled drivers.

5. Add any additional Python dependencies from the driver's instructions to
   the root-owned `/etc/weewx-php-ingest/requirements.txt`. If needed, install
   them into the active environment with this command (updating to the same
   collector version does not reinstall dependencies):

   ```sh
   sudo /opt/weewx-php-ingest/current/.venv/bin/python -m pip install -r /etc/weewx-php-ingest/requirements.txt
   ```

   Then check and start:

   ```sh
   sudo -u weewx-ingest weewx-php-ingest check
   sudo systemctl restart weewx-php-ingest
   sudo journalctl -u weewx-php-ingest -f
   ```

   `check` validates the configuration; only the running service reads hardware.

Drivers under `/var/lib/weewx-php-ingest/bin/user/` survive collector updates.
The WeeWX sync action does not update their versions; follow each driver's
update instructions. Extensions with additional services also require the
explicit `Ingest.Services` configuration described below.

### Auxiliary services and Python environments

Additional Python import directories use a comma-separated `python_paths` value
in the station's Ingest section. An absolute `python` executable can select a
separate environment; that environment must contain the collector and its required
packages and is maintained separately from the default managed environment.

The collector supplies empty engine service groups. Driver callbacks still run;
archive storage, reporting and external uploads are disabled. Explicit auxiliary
services for a single station use:

```ini
[Ingest]
    # Other required Ingest settings remain here.
    [[Services]]
        prep_services = weewx.engine.StdTimeSynch
        process_services = user.myservice.RequiredService
```

For multiple stations, place `[[[[Services]]]]` beneath that station's
`[[[Ingest]]]`. `StdTimeSynch` may set the device clock. Document any corrections
applied by auxiliary services to avoid repeating them in PHP.

`lifecycle_interval` and `lifecycle_delay` coordinate END_ARCHIVE_PERIOD, POST_LOOP
and PRE_LOOP without reading hardware archives. They do not set the PHP archive
interval. String metadata must be explicitly excluded, for example
`exclude_fields = source`. Invalid packet fields or values stop that worker with
`invalid_packet`; correct the driver/configuration and restart.

## Storage, retry and recovery

`max_events` and `spool_max_bytes` in a single station's `[Ingest]` bound its queue.
For multiple stations, use `max_events` and `max_bytes` in each `[[[Ingest]]]`.
Both quotas include quarantined events. Top-level `max_bytes` bounds HTTP requests.
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
python -m pip install ./vendor/weewx -e '.[test]'
python scripts/sync_weewx.py --check
python scripts/verify_install.py .
python -m pytest -q
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m pip_audit --skip-editable
python -m build
```

In a disposable Linux environment, `sudo bash tests/installer_smoke.sh "$PWD"`
checks installation, updates, repeated updates and failure preservation.

The optional PHP integration test needs the separate project, PHP and SQLite3:

```sh
WEEWX_PHP_ROOT=/path/to/weewx-php python -m pytest -q tests/test_php_integration.py
```

It starts isolated local HTTP/TLS test servers and creates a temporary receiver
configuration/database. It does not modify PHP project files or use production
credentials. Without `WEEWX_PHP_ROOT`, that one test is skipped.
