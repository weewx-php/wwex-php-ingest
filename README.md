# weewx-php-ingest

Collect weather station readings with WeeWX drivers and send them to a remote
weewx-php server over HTTPS. Each station has its own worker and persistent SQLite
queue. Network failures are retried without changing event IDs.

## Install

Raspberry Pi OS Bookworm or newer, Debian 12+, or Ubuntu 24.04+. Python 3.11+
and systemd are required. In a checkout of this repository, run:

```sh
sudo bash install.sh --source "$PWD"
```

The installer installs dependencies, all bundled WeeWX drivers and the systemd
service, generates a random token and collector ID, then opens the setup assistant.

The assistant asks for the destination domain, scans connected USB/serial devices,
offers the bundled hardware drivers and their connection settings, and tests a real
reading. It supports multiple stations and starts the service when setup finishes.
Reopen it at any time:

```sh
sudo weewx-php-ingest configure
```

The first upload appears in the destination's existing station list. Adopt the
station there. No token copying or collector registration is required. The PHP host
must have ingest enabled and the matching native discovery support.

This repository is currently private. Fetching it requires GitHub access, including
for subsequent updates. For SSH access, install with
`--repository git@github.com:weewx-php/wwex-php-ingest.git`; the account running the
installer must be able to clone that URL.

For a public repository, installation directly from GitHub is one command:

```sh
curl -fsSL https://raw.githubusercontent.com/weewx-php/wwex-php-ingest/main/install.sh | sudo bash
```

## Configure

All settings live in `/etc/weewx-php-ingest/weewx.conf`:

```ini
[Station]
    station_type = Vantage
    latitude = 52.52
    longitude = 13.405
    altitude = 40, meter

[Vantage]
    driver = weewx.drivers.vantage
    type = serial
    port = /dev/serial/by-id/YOUR_CONSOLE
    baudrate = 19200

[Ingest]
    collector_id = YOUR_COLLECTOR_ID
    url = https://weather.example.org/ingest/weewx.php
    token = YOUR_TOKEN
    state_dir = /var/lib/weewx-php-ingest
    station_key = station
    send_interval = 10
```

Driver sections use the usual WeeWX syntax and options. The assistant uses the
configuration editors supplied by the bundled drivers.
See the [single-station example](examples/weewx.conf) and
[multiple-station example](examples/weewx-multi.conf). Multiple instances of the
same driver are supported. The configuration has mode `0600` and belongs to
`weewx-ingest`.

After editing, validate and restart:

```sh
sudo -u weewx-ingest weewx-php-ingest check
sudo systemctl restart weewx-php-ingest
```

Alternatively, adopt the station with the PHP CLI:

```sh
php bin/weewx-php ingest list
php bin/weewx-php ingest adopt <sender> "Garden"
```

## Update

```sh
sudo weewx-php-ingest update
```

Updates the collector and bundled drivers together from `main`. The new version is
built and checked before switching the service. Configuration, station IDs and
queued readings are preserved. A failed service start restores the previous version.

## Drivers

`vendor/weewx/` contains the official WeeWX source release, including every bundled
hardware driver and the matching engine. The installer uses this source directly.
The **Sync WeeWX drivers** GitHub Action checks daily for a newer stable release,
verifies its checksum, runs the tests and commits successful updates to `main`.
Stations receive those updates with the update command above.

Third-party drivers are installed separately; their files and requirements can be
kept outside the managed release. See [operation and extensions](docs/operations.md)
and [upstream license and provenance](THIRD_PARTY.md).

## Status

```sh
sudo -u weewx-ingest weewx-php-ingest status
sudo journalctl -u weewx-php-ingest -f
```

The collector sends original LOOP observations; hardware archive recovery and
rollups require a later protocol. Physical Davis/USB hardware has not been tested.

- [Receiver contract](docs/native-ingest-v1.md)
- [Implementation and tests](docs/implementation.md)
- [Security review](docs/reviews/collector-security.md)

The receiver integration is tracked in [weewx-php-adoption.patch](integrations/weewx-php-adoption.patch).
