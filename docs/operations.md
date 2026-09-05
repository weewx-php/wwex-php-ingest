# Deployment and operation

## Installation and service

Direkt installieren; die geführte Einrichtung startet anschließend:

```sh
curl -fsSL https://raw.githubusercontent.com/weewx-php/wwex-php-ingest/main/install.sh | sudo bash
```

Alternativ aus einem Repository-Checkout: `sudo bash install.sh --source "$PWD"`.
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
are preserved. Das Repository ist öffentlich; Installation und Updates über HTTPS
benötigen keine GitHub-Anmeldung.

### Unbeaufsichtigte Installation

Mit `--non-interactive` wird die Einrichtung übersprungen:

```sh
curl -fsSL https://raw.githubusercontent.com/weewx-php/wwex-php-ingest/main/install.sh | sudo bash -s -- --non-interactive
```

Bei einer neuen Installation bleibt der Dienst bis zur Einrichtung gestoppt.

### Einrichtung und Diagnose

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

### Treiber von GitHub installieren

Die mitgelieferte `weectl`-CLI liegt unter
`/opt/weewx-php-ingest/current/.venv/bin/weectl`. Sie installiert
[WeeWX-Erweiterungen](https://weewx.com/docs/latest/custom/extensions/)
aus ZIP-/Tarball-URLs, lokalen Archiven oder Verzeichnissen. Verwende die
Download-URL aus der Treiberanleitung; eine GitHub-Repository-Seite reicht nicht.
Das Paket muss einen WeeWX-Installer (`install.py`) enthalten.

1. Dienst stoppen und Konfiguration öffnen:

   ```sh
   sudo systemctl stop weewx-php-ingest
   sudoedit /etc/weewx-php-ingest/weewx.conf
   ```

2. Die folgenden Werte auf oberster Ebene ergänzen, soweit sie fehlen.
   `WEEWX_ROOT` und `USER_ROOT` stehen vor dem ersten Abschnitt.
   Vorhandene Abschnitte erweitern, nicht doppelt anlegen. WeeWX benötigt
   `StdReport` beim Zusammenführen von Erweiterungsoptionen; dadurch werden
   keine Reports im Collector aktiviert.

   ```ini
   WEEWX_ROOT = /var/lib/weewx-php-ingest
   USER_ROOT = bin/user

   [StdReport]
       HTML_ROOT = public_html
       SKIN_ROOT = skins

   [Engine]
       [[Services]]
   ```

3. Paket installieren. `OWNER`, `REPO` und `VERSION` durch die Angaben des
   Treibers ersetzen. Der Aufruf schützt die neu geschriebene Token-Konfiguration
   und macht installierte Python-Dateien für den Dienst lesbar:

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

4. In der `weewx.conf` den Treiberabschnitt laut dessen Anleitung ergänzen und
   unter `[Station]` den passenden `station_type` setzen. Beispiel mit Platzhaltern:

   ```ini
   [Station]
       station_type = DRIVER_NAME
       # Vorhandene latitude, longitude und altitude beibehalten.

   [DRIVER_NAME]
       driver = user.DRIVER_MODULE
       # Verbindungsoptionen laut Treiberanleitung.
   ```

   Bei mehreren Stationen gehören diese Einstellungen in die jeweilige
   `[Stations]`-Untersektion. Von `weectl` ergänzte Treiberoptionen stehen zunächst
   auf oberster Ebene und müssen dort übernommen werden. Der geführte Assistent
   bietet derzeit nur die mitgelieferten Treiber an.

5. Zusätzliche Python-Abhängigkeiten laut Treiberanleitung in die root-eigene
   `/etc/weewx-php-ingest/requirements.txt` eintragen. Falls erforderlich, mit
   `sudo weewx-php-ingest update` installieren. Danach prüfen und starten:

   ```sh
   sudo -u weewx-ingest weewx-php-ingest check
   sudo systemctl restart weewx-php-ingest
   sudo journalctl -u weewx-php-ingest -f
   ```

   `check` prüft die Konfiguration; erst der laufende Dienst liest die Hardware.

Treiber unter `/var/lib/weewx-php-ingest/bin/user/` bleiben bei Collector-Updates
erhalten. Ihre Version wird durch die WeeWX-Sync-Action nicht aktualisiert;
verwende dafür die Update-Anleitung des jeweiligen Treibers. Erweiterungen mit
zusätzlichen Diensten benötigen außerdem die unten beschriebene explizite
`Ingest.Services`-Konfiguration.

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
