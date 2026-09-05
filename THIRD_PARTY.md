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
