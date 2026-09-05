#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root in a disposable Linux environment.' >&2; exit 1; }
input=$(realpath "${1:-.}")
work=$(mktemp -d /tmp/ingest-install-test.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
mkdir "$work/source"
tar --exclude=.git --exclude=.venv --exclude=dist --exclude=.test-tmp --exclude=.ruff_cache \
    --exclude=__pycache__ --exclude='*.egg-info' -C "$input" -cf - . | tar -xf - -C "$work/source"
git init --quiet -b main "$work/source"
git -C "$work/source" config user.name 'Installer test'
git -C "$work/source" config user.email 'installer-test@example.invalid'
git -C "$work/source" add .
git -C "$work/source" commit --quiet -m initial
args=(--source "$work/source" --prefix "$work/install" --config "$work/weewx.conf"
      --state-dir "$work/state" --no-service --non-interactive)
bash "$work/source/install.sh" "${args[@]}"
cli="$work/install/current/.venv/bin/weewx-php-ingest"
python="$work/install/current/.venv/bin/python"
"$python" - "$work/weewx.conf" <<'PY'
import sys
from configobj import ConfigObj
cfg = ConfigObj(sys.argv[1])
cfg['Ingest']['collector_id'] = '11111111-1111-1111-1111-111111111111'
cfg['Ingest']['token'] = 'a' * 64
cfg['Ingest']['url'] = 'https://weather.example.org/ingest/weewx.php'
cfg.write()
PY
"$cli" --config "$work/weewx.conf" init >"$work/before.json"
cp "$work/weewx.conf" "$work/before.conf"
"$python" - "$work/weewx.conf" <<'PY'
import sys, time
from weewx_php_ingest.config import load_config
from weewx_php_ingest.protocol import event_from_loop
from weewx_php_ingest.supervisor import open_spools
spool = open_spools(load_config(sys.argv[1]))[0]
spool.append(event_from_loop({'dateTime':int(time.time()), 'usUnits':17, 'rain':0.2},
                            spool.station_id, 'weewx.drivers.simulator'))
spool.close()
PY
old=$(readlink "$work/install/current")
git -C "$work/source" commit --quiet --allow-empty -m update
# Exercise the installed update command and remote-fetch path against the local fixture repository.
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0="url.file://$work/source.insteadOf" \
    GIT_CONFIG_VALUE_0=https://github.com/weewx-php/wwex-php-ingest.git "$cli" update
[[ $(readlink "$work/install/current") != "$old" ]]
cmp "$work/weewx.conf" "$work/before.conf"
"$cli" --config "$work/weewx.conf" status >"$work/after.json"
"$python" - "$work/before.json" "$work/after.json" <<'PY'
import json, sys
before, after = [json.load(open(p)) for p in sys.argv[1:]]
assert before['collector_id'] == after['collector_id']
assert before['stations'][0]['station_id'] == after['stations'][0]['station_id']
assert after['stations'][0]['events'] == 1
PY
# Repeating the same update is a no-op.
current=$(readlink "$work/install/current")
bash "$work/source/install.sh" "${args[@]}" --update
[[ $(readlink "$work/install/current") == "$current" ]]
# An invalid configuration must not replace the current version.
sed -i 's|https://weather.example.org|http://weather.example.org|' "$work/weewx.conf"
git -C "$work/source" commit --quiet --allow-empty -m invalid-config-update
if bash "$work/source/install.sh" "${args[@]}" --update; then
    echo 'Invalid configuration update unexpectedly succeeded.' >&2; exit 1
fi
[[ $(readlink "$work/install/current") == "$current" ]]
# Check guided setup and real Simulator access as the service account.
groupadd --system weewx-ingest
useradd --system --gid weewx-ingest --shell /usr/sbin/nologin weewx-ingest
chown root:weewx-ingest "$work"
chmod 0750 "$work"
chown -R weewx-ingest:weewx-ingest "$work/state"
chown weewx-ingest:weewx-ingest "$work/weewx.conf"
printf '%s\n' 'https://127.0.0.1:9/ingest/weewx.php' 'Simulator' 'n' 'n' \
    | "$cli" --config "$work/weewx.conf" configure
[[ $(stat -c %U "$work/state/stations/station.sqlite3") == weewx-ingest ]]
[[ $(stat -c %U "$work/weewx.conf") == weewx-ingest ]]
[[ $(stat -c %a "$work/weewx.conf") == 600 ]]
printf '%s\n' 'Installer smoke: install, CLI update, no-op, failure preservation and guided setup passed.'
