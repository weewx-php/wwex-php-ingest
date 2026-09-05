#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

ROOT=/opt/weewx-php-ingest
CONFIG=/etc/weewx-php-ingest/weewx.conf
STATE=/var/lib/weewx-php-ingest
REPOSITORY=https://github.com/weewx-php/wwex-php-ingest.git
SOURCE=
UPDATE=0
INTERACTIVE=1
SERVICE=1

die() { printf '%s\n' "$*" >&2; exit 1; }
while (($#)); do
    case "$1" in
        --update) UPDATE=1; shift ;;
        --non-interactive) INTERACTIVE=0; shift ;;
        --no-service) SERVICE=0; shift ;;
        --source|--prefix|--config|--state-dir|--repository)
            (($# >= 2)) || die "Missing value: $1"
            case "$1" in
                --source) SOURCE=$2 ;;
                --prefix) ROOT=$2 ;;
                --config) CONFIG=$2 ;;
                --state-dir) STATE=$2 ;;
                --repository) REPOSITORY=$2 ;;
            esac
            shift 2 ;;
        --help)
            printf '%s\n' 'sudo bash install.sh [--source CHECKOUT] [--non-interactive]' \
                'sudo weewx-php-ingest update'
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

[[ $(uname -s) == Linux ]] || die 'Linux required.'
[[ $EUID == 0 ]] || die 'Run with sudo.'
validate_paths() {
    for path in "$ROOT" "$CONFIG" "$STATE"; do
        [[ $path =~ ^/[A-Za-z0-9_./-]+$ && $path != / && $path != */../* && $path != */.. ]] \
            || die 'Use absolute paths containing letters, digits, underscores, dots or hyphens.'
    done
    [[ ! -L $ROOT && ! -L $CONFIG && ! -L $STATE ]] || die 'Symlinked install/config/state paths refused.'
}
validate_paths
command -v apt-get >/dev/null || die 'Debian, Ubuntu or Raspberry Pi OS required.'
if ! command -v git >/dev/null || ! command -v python3 >/dev/null \
        || ! python3 -c 'import ensurepip, venv; assert __import__("sys").version_info >= (3,11)' 2>/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git ca-certificates python3 python3-venv
fi
python3 -c 'import sys; assert sys.version_info >= (3,11)' \
    || die 'Python 3.11+ required (Raspberry Pi OS Bookworm or newer).'
install -d -m 0755 "$ROOT" "$ROOT/releases"
[[ $(stat -c %u "$ROOT") == 0 ]] || die 'Install directory must belong to root.'
chmod go-w "$ROOT" "$ROOT/releases"

# The installed command reads its root-owned deployment settings, not the weather configuration.
if ((UPDATE)) && [[ -f $ROOT/deployment.json ]]; then
    mapfile -t settings < <(python3 - "$ROOT/deployment.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
for key in ('config', 'state', 'repository'):
    print(data[key])
print(int(data['service']))
PY
)
    ((${#settings[@]} == 4)) || die 'Invalid deployment settings.'
    CONFIG=${settings[0]}; STATE=${settings[1]}; REPOSITORY=${settings[2]}
    [[ ${settings[3]} == 0 || ${settings[3]} == 1 ]] || die 'Invalid service setting.'
    SERVICE=${settings[3]}
fi
validate_paths
case "$REPOSITORY" in
    https://github.com/weewx-php/wwex-php-ingest.git|git@github.com:weewx-php/wwex-php-ingest.git) ;;
    *) die 'Unexpected repository URL.' ;;
esac

if [[ -z $SOURCE ]]; then
    download=$(mktemp -d "$ROOT/download.XXXXXXXX")
    trap 'rm -rf -- "$download"' EXIT
    git clone --quiet --depth 1 --branch main "$REPOSITORY" "$download/source"
    args=(--source "$download/source" --prefix "$ROOT" --config "$CONFIG" --state-dir "$STATE"
          --repository "$REPOSITORY")
    ((UPDATE)) && args+=(--update)
    ((INTERACTIVE)) || args+=(--non-interactive)
    ((SERVICE)) || args+=(--no-service)
    bash "$download/source/install.sh" "${args[@]}"
    exit $?
fi

SOURCE=$(realpath "$SOURCE")
git_source() { git -c safe.directory="$SOURCE" -C "$SOURCE" "$@"; }
git_source diff --quiet && git_source diff --cached --quiet \
    || die 'Commit source changes before installation.'
revision=$(git_source rev-parse HEAD)
[[ $revision =~ ^[0-9a-f]{40}$ ]] || die 'Invalid source revision.'
exec 9>"$ROOT/install.lock"
flock -n 9 || die 'An installation or update is already running.'
radio_changed=0
if ((UPDATE)); then
    radio_before=$(dpkg-query -W -f='${Version}' rtl-433 2>/dev/null || true)
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rtl-433
    radio_after=$(dpkg-query -W -f='${Version}' rtl-433)
    [[ $radio_before == "$radio_after" ]] || radio_changed=1
fi
if ((UPDATE)) && [[ -f $ROOT/current/REVISION ]] && [[ $(cat "$ROOT/current/REVISION") == "$revision" ]]; then
    if ((radio_changed && SERVICE)) && [[ -f /etc/systemd/system/weewx-php-ingest.service ]]; then
        systemctl try-restart weewx-php-ingest
    fi
    printf 'Current: %s\n' "$revision"
    exit 0
fi

# Install native build/USB requirements once. No existing Python environment is modified.
if [[ ! -f $ROOT/system-dependencies ]] || ! command -v rtl_433 >/dev/null; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-dev build-essential libusb-1.0-0 rtl-433
    touch "$ROOT/system-dependencies"
fi
release=$(mktemp -d "$ROOT/releases/$revision.XXXXXXXX")
chmod 0755 "$release"
install -d "$release/source"
git_source archive "$revision" | tar -x -C "$release/source"
chmod -R go-w "$release/source"
python3 "$release/source/scripts/sync_weewx.py" --check
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install --disable-pip-version-check \
    "$release/source/vendor/weewx" "$release/source"
if [[ -f $(dirname "$CONFIG")/requirements.txt ]]; then
    "$release/.venv/bin/python" -m pip install --disable-pip-version-check \
        -r "$(dirname "$CONFIG")/requirements.txt"
fi
"$release/.venv/bin/python" "$release/source/scripts/verify_install.py" "$release/source"
"$release/.venv/bin/python" -m pip check
printf '%s\n' "$revision" >"$release/REVISION"

if ((SERVICE)); then
    getent group weewx-ingest >/dev/null || groupadd --system weewx-ingest
    id weewx-ingest >/dev/null 2>&1 || useradd --system --gid weewx-ingest \
        --home-dir "$STATE" --shell /usr/sbin/nologin weewx-ingest
    for group in dialout plugdev; do
        if getent group "$group" >/dev/null; then usermod -a -G "$group" weewx-ingest; fi
    done
    install -d -m 0700 -o weewx-ingest -g weewx-ingest "$STATE"
    install -d -m 0750 -o root -g weewx-ingest "$(dirname "$CONFIG")"
else
    install -d -m 0700 "$STATE" "$(dirname "$CONFIG")"
fi

if [[ ! -e $CONFIG ]]; then
    "$release/.venv/bin/weewx-php-ingest" --config "$CONFIG" _initialize --state-dir "$STATE"
fi
chmod 0600 "$CONFIG"
if ((SERVICE)); then chown weewx-ingest:weewx-ingest "$CONFIG"; fi
configured=0
if "$release/.venv/bin/weewx-php-ingest" --config "$CONFIG" check; then configured=1; fi
# Refuse an update with invalid configuration before touching the running release.
if ((UPDATE && !configured)); then die 'Configuration check failed; current version unchanged.'; fi

old_current=$(readlink "$ROOT/current" || true)
old_active=0
unit=/etc/systemd/system/weewx-php-ingest.service
unit_backup="$release/previous.service"
if ((SERVICE)); then
    systemctl is-active --quiet weewx-php-ingest && old_active=1
    if [[ -f $unit ]]; then cp -p "$unit" "$unit_backup"; fi
fi
activated=0
stopped=0
rollback() {
    code=$?
    trap - EXIT
    if ((code != 0 && (activated || stopped))); then
        if ((activated)) && [[ -n $old_current ]]; then
            ln -s "$old_current" "$ROOT/rollback.$$"
            mv -Tf "$ROOT/rollback.$$" "$ROOT/current"
        elif ((activated)); then
            rm -f -- "$ROOT/current"
        fi
        if ((SERVICE)); then
            if [[ -f $unit_backup ]]; then cp -p "$unit_backup" "$unit"; else rm -f -- "$unit"; fi
            systemctl daemon-reload
            if ((old_active)); then systemctl start weewx-php-ingest; fi
        fi
        printf '%s\n' 'Update failed; previous version restored.' >&2
    fi
    exit "$code"
}
trap rollback EXIT
if ((SERVICE && old_active)); then
    stopped=1
    systemctl stop weewx-php-ingest
fi
ln -s "$release" "$ROOT/activate.$$"
mv -Tf "$ROOT/activate.$$" "$ROOT/current"
activated=1
python3 - "$ROOT/deployment.json" "$CONFIG" "$STATE" "$REPOSITORY" "$SERVICE" <<'PY'
import json, os, sys
path, config, state, repository, service = sys.argv[1:]
temporary = path + '.new'
with open(temporary, 'w') as stream:
    json.dump(dict(config=config, state=state, repository=repository, service=bool(int(service))), stream)
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
if ((SERVICE)); then
    sed -e "s|/opt/weewx-php-ingest|$ROOT|g" \
        -e "s|/etc/weewx-php-ingest/weewx.conf|$CONFIG|g" \
        -e "s|/var/lib/weewx-php-ingest|$STATE|g" \
        "$release/source/deploy/weewx-php-ingest.service" >"$unit"
    chmod 0644 "$unit"
    ln -sfn "$ROOT/current/.venv/bin/weewx-php-ingest" /usr/local/bin/weewx-php-ingest
    systemctl daemon-reload
    if ((configured)); then
        systemctl enable weewx-php-ingest
        systemctl start weewx-php-ingest
        sleep 3
        systemctl is-active --quiet weewx-php-ingest
    fi
fi
activated=0
stopped=0
printf 'Installed: %s\nConfiguration: %s\n' "$revision" "$CONFIG"
if ((!configured)); then
    if ((INTERACTIVE)) && [[ -r /dev/tty && -w /dev/tty ]]; then
        "$ROOT/current/.venv/bin/weewx-php-ingest" --config "$CONFIG" configure --state-dir "$STATE" </dev/tty
    else
        printf '%s\n' 'Configure: sudo weewx-php-ingest configure'
    fi
fi
