#!/usr/bin/env bash
# phonon-ntp — privileged helper for the NTP write path.
#
# Installed to /usr/local/sbin/phonon-ntp by install.sh. Granted
# NOPASSWD sudo via /etc/sudoers.d/phonon-stage. Called from the
# phonon-stage daemon (running unprivileged) via `sudo -n`.
#
# Two subcommands:
#   write-sources <body>   atomically replace the phonon-managed
#                          chrony drop-in then reload chrony
#   sync                   run `chronyc -a burst` + `makestep` to
#                          fast-resync the local clock
#
# Each subcommand is independently atomic. write-sources writes to
# a tempfile, fsyncs, renames into place (mv = atomic on same FS).
# If `chronyc reload sources` fails, we restore the previous file
# so we never leave chrony with a broken config.

set -euo pipefail

DROPIN=/etc/chrony/sources.d/phonon-servers.conf
LOG=/var/log/phonon/ntp.log
mkdir -p "$(dirname "$LOG")"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

cmd_write_sources() {
    local body="$1"
    if [ -z "$body" ]; then
        log "write-sources called with empty body — refusing"
        return 2
    fi
    mkdir -p "$(dirname "$DROPIN")"
    local backup=""
    if [ -f "$DROPIN" ]; then
        backup="${DROPIN}.bak.$(date +%s)"
        cp -p "$DROPIN" "$backup"
    fi
    local tmp
    tmp="$(mktemp --tmpdir="$(dirname "$DROPIN")" phonon-ntp.XXXXXX)"
    # shellcheck disable=SC2310 — we want the trap to catch interrupts
    trap 'rm -f "$tmp"' EXIT
    printf '%s' "$body" > "$tmp"
    sync -- "$tmp"
    chown root:root "$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$DROPIN"
    trap - EXIT
    log "wrote $DROPIN ($(wc -l <"$DROPIN") lines)"
    # Tell chrony to re-read sources. Falls back to a full service
    # restart if `chronyc reload sources` isn't available on this
    # build (older chrony lacks it).
    if chronyc -a reload sources 2>>"$LOG"; then
        log "chronyc reload sources OK"
    else
        log "chronyc reload failed — trying systemctl restart chrony"
        if ! systemctl restart chrony 2>>"$LOG"; then
            log "restart failed — restoring previous config"
            if [ -n "$backup" ] && [ -f "$backup" ]; then
                mv -f "$backup" "$DROPIN"
                systemctl restart chrony || true
            fi
            return 3
        fi
    fi
    [ -n "$backup" ] && rm -f "$backup"
    return 0
}

cmd_sync() {
    # `burst 4/4` = 4 requests now, accept ≥4 responses. `-a` =
    # authenticate via the local control socket (no key needed; works
    # because we run as root). Followed by makestep to force a step
    # if the resulting offset is large.
    chronyc -a burst 4/4 2>>"$LOG"
    sleep 2
    chronyc -a makestep 2>>"$LOG"
    log "sync requested"
}

case "${1:-}" in
    write-sources)
        shift
        cmd_write_sources "${1:-}"
        ;;
    sync)
        cmd_sync
        ;;
    *)
        echo "usage: $0 {write-sources <body> | sync}" >&2
        exit 1
        ;;
esac
