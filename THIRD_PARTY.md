# Bundled WeeWX

`vendor/weewx/` contains the unmodified official WeeWX source distribution,
including its hardware drivers, engine and extension installer. Copyright and
license notices are preserved in every upstream file and in
`vendor/weewx/LICENSE.txt` (GPL-3.0).

`vendor/weewx-source.json` records the release, download URL, archive SHA-256 and
file checksums. `scripts/sync_weewx.py --check` verifies this snapshot. The system
installer installs WeeWX from this local source, together with the collector.

Updates use official stable releases from the WeeWX project on PyPI. The scheduled
workflow updates the complete matching source and the collector's dependency pin.
No individual development-branch drivers are mixed into an older engine.

## PurpleAir test fixture

`tests/fixtures/purple_upstream/` contains an unmodified copy of the PurpleAir
service for offline integration tests, with its original GPL-3.0 license.
See its [provenance and checksum](tests/fixtures/purple_upstream/README.md).
It is not installed into the collector's runtime environment.

The same test-only approach is used for [AirGradient](tests/fixtures/airgradient_upstream/README.md),
[AirLink](tests/fixtures/airlink_upstream/README.md) and [air-Q](tests/fixtures/airq_upstream/README.md).
Each directory preserves the unmodified source, upstream license, revision and checksum.


## GW1000 runtime parser

`src/weewx_php_ingest/_vendor/gw1000.py` is the unmodified upstream GW1000 module
by Gary Roderick, licensed under GPL-3.0-or-later. The collector uses its
`ApiParser` behind a bounded read-only socket client. Its standard driver and
service are not started. Source revision and SHA-256 are recorded in
`gw1000-source.json`; the upstream license is `LICENSE-gw1000` alongside it.
This copy is installed with the collector and works without an upstream download.
