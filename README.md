# weewx-php-ingest

Hardware collection for a remote [weewx-php](../weewx-php/README.md) installation.
Runs on a Raspberry Pi or another computer in the station owner's LAN.

```text
Davis / other hardware -> WeeWX drivers -> local spool -> HTTPS -> weewx-php
                         Raspberry Pi                           web host
```

This is a separate Python project. The web host runs PHP; it needs no Python,
USB access or connection into the home network.

## Implemented

- Use original WeeWX drivers, including third-party extensions.
- Run independent driver instances concurrently, including the same driver twice.
- Identify every instance as a separate station in the remote live journal.
- Persist readings locally and retry delivery after network or server outages.
- Configure the send interval in seconds independently of archive intervals.
- Run without generating reports, images or Cheetah templates.

## Status

The v1 Python collector is implemented with real WeeWX 5.5.0 engine processes,
SQLite WAL spools with `synchronous=FULL`, independent station identities,
HTTPS batching, fair retries and process watchdogs. The native PHP receiver is
implemented in the separate weewx-php project.

- [Implemented receiver contract](docs/native-ingest-v1.md)
- [Request JSON Schema](schemas/weewx-v1.schema.json)
- [PHP security review](../weewx-php/docs/reviews/native-ingest-security.md)

Version 1 sends original LOOP events in batches to `POST /ingest/weewx.php`.
Every driver instance has a persistent station UUID. Only per-event `stored`
or `duplicate` acknowledgements release the local spool entry. Hardware
archive records and accumulator rollups require a later protocol version.

[Architecture and implementation sequence](docs/architecture.md) documents the
metadriver review, process isolation, transport, replay, hardware history and
optional accumulator rollups. PHP receiver changes belong in the separate
weewx-php project.

## Install

Python 3.11 or newer. Linux/Raspberry Pi is the deployment target; the tests also
run on Windows. Install device-specific libraries and permissions required by
your WeeWX driver.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
```

On Windows, use `.venv\Scripts\python.exe` and `.venv\Scripts\weewx-php-ingest.exe`.

## Configure and start

1. Enable native ingest on the PHP host and provision the collector:

   ```sh
   php bin/weewx-php collector add "Raspberry"
   ```

2. Copy [examples/collector.toml](examples/collector.toml) and the required WeeWX
   `.conf` files into your configuration directory. Set the returned collector
   ID, HTTPS endpoint and an absolute writable `state_dir`. File paths are
   relative to `collector.toml`; WeeWX roots are relative to its `.conf`.

3. Put the returned token alone in `collector.token`, with mode `0600` on Linux.
   Alternatively set `token_env` to an environment variable's name and remove
   `token_file`. The example ID and endpoint are placeholders.

4. Validate, create the station identities, and start:

   ```sh
   weewx-php-ingest --config /etc/weewx-php-ingest/collector.toml check
   weewx-php-ingest --config /etc/weewx-php-ingest/collector.toml init
   weewx-php-ingest --config /etc/weewx-php-ingest/collector.toml run
   ```

5. After the first upload, admit each station on the PHP host:

   ```sh
   php bin/weewx-php collector stations <collector_id>
   php bin/weewx-php collector adopt <collector_id> <station_id> "Garden"
   ```

   Pending stations retain their readings and retry after at least 60 seconds.
   Configure archive sender assignment and schedule PHP's tick as described in
   the [receiver contract](docs/native-ingest-v1.md).

## Operations

```sh
weewx-php-ingest --config /etc/weewx-php-ingest/collector.toml status
weewx-php-ingest --config /etc/weewx-php-ingest/collector.toml upload-once
weewx-php-ingest --config /etc/weewx-php-ingest/collector.toml retry garden
```

`status` prints station UUIDs, queue sizes, oldest unconfirmed times, full states,
quarantine reasons, last collection/delivery, worker PID/restarts and retry times.
It does not open hardware or read the token. `upload-once` sends one due batch and
respects backoff; stop the service first because only one uploader may run.
`retry` requeues quarantined events without changing their IDs or data.

Change `label` freely. Preserve `[stations.<key>]` keys and spool files across
renames, USB changes and upgrades. Changing the driver module or collector ID for
an existing spool is refused. Stop the collector before copying the complete
state directory, configurations and token to a replacement computer.

See [deployment and operation](docs/operations.md) for systemd, extensions,
capacity planning, failure handling and verification commands.

## Verification and scope

Tests cover real Simulator processes, callbacks, process failure/silence,
quota pause/resume, durable retries, TLS and the real PHP receiver. Physical
Davis/USB reconnect and arbitrary third-party hardware remain untested.
The collector cannot recover LOOP data produced while collection was stopped.
Hardware history and rollups require a later receiver protocol. WeeWX's reporting
dependencies remain installed, but report/template services are not started.

- [Implementation and test notes](docs/implementation.md)
- [Collector security review](docs/reviews/collector-security.md)
