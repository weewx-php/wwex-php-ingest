# weewx-php-ingest

Reads weather stations using WeeWX drivers and sends observations to weewx-php.

## Install

Requires Debian 12+, Ubuntu 24.04+, or Raspberry Pi OS Bookworm+ with Python 3.11+ and systemd.

```sh
curl -fsSL https://raw.githubusercontent.com/weewx-php/wwex-php-ingest/main/install.sh | sudo bash
```

Installs the collector, all bundled WeeWX drivers, and the service.
The token and collector ID are generated automatically.

No manual download or GitHub account required.

## Set up

Guided setup starts after installation:

1. Enter the destination domain.
2. Scan connected USB/serial devices and select your hardware.
3. Configure the connection and test a reading.
4. Click **Adopt** for the station in the weewx-php web interface.

No token copying or registration required. The PHP receiver needs ingest enabled
and [native station discovery](integrations/weewx-php-adoption.patch).

Run setup again:

```sh
sudo weewx-php-ingest configure
```

Driver settings, URL, and token are stored in `/etc/weewx-php-ingest/weewx.conf`.
[Example configuration](examples/weewx.conf) · [Multiple stations](examples/weewx-multi.conf)

## Virtual stations

Select **Virtual**, then **PurpleAir**, **AirGradient**, **AirLink** or **air-Q**.
Each instance sends only its service's measurements and is adopted separately.

[Service installation, setup and examples](docs/virtual-stations.md)

## RTL-SDR sensors

Select **RTL433** during setup. `rtl-433` is installed automatically.
Each identified sensor appears as a separate station in PHP: **adopt, then map fields**.
One USB receiver serves all its sensors. New sensors appear automatically.

[Configuration and contract](docs/native-ingest-v3.md) · [Example](examples/weewx-sdr.conf)

## GW1000 and WeatherFlow / Tempest

Select **GW1000** or **WeatherFlowUDP** during setup.
GW1000 discovers the gateway on your LAN or accepts its IPv4 address.
WeatherFlow listens on UDP port 50222; the hub must be on the same LAN.
Each detected sensor/device appears in PHP: **adopt, then map fields**.

[Configuration example](examples/weewx-network.conf) · [Sensor handling and limits](docs/native-ingest-v3.md#gw1000-lan-sensors)

## Hardware archive

```ini
[StdArchive]
    record_generation = hardware
```

Sends original logger records alongside LOOP readings. Unsupported drivers fall back
to LOOP collection. Logger, upload and PHP archive intervals are independent. PHP keeps original
logger intervals, combines complete spans and exposes coarse history alongside
finer gaps without inventing measurements.

[Contract and recovery](docs/native-ingest-v2.md) · [Request schema](schemas/weewx-v2.schema.json)

## Update

```sh
sudo weewx-php-ingest update
```

Updates the collector and bundled drivers. A GitHub Action checks daily for new
stable WeeWX releases. Configuration and queued observations are preserved.
A separate daily Action checks the bundled GW1000 parser and tests updates before publishing.
Also updates `rtl-433` from the configured OS repositories.

## Additional drivers from GitHub

The bundled WeeWX CLI supports `weectl extension install` with a GitHub ZIP/tarball
URL. The package must be a WeeWX extension containing `install.py`.

[Installation and configuration steps](docs/operations.md#installing-drivers-from-github).
Select additional drivers in `weewx.conf`; guided setup lists only bundled drivers.
Third-party drivers are updated separately.

[Operation and troubleshooting](docs/operations.md) · [WeeWX license](THIRD_PARTY.md)
