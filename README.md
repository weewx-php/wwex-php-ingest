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

## Update

```sh
sudo weewx-php-ingest update
```

Updates the collector and bundled drivers. A GitHub Action checks daily for new
stable WeeWX releases. Configuration and queued observations are preserved.

## Additional drivers from GitHub

The bundled WeeWX CLI supports `weectl extension install` with a GitHub ZIP/tarball
URL. The package must be a WeeWX extension containing `install.py`.

[Installation and configuration steps](docs/operations.md#treiber-von-github-installieren).
Select additional drivers in `weewx.conf`; guided setup lists only bundled drivers.
Third-party drivers are updated separately.

[Operation and troubleshooting](docs/operations.md) · [WeeWX license](THIRD_PARTY.md)
