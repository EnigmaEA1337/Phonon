#!/usr/bin/env bash
# phonon-net — privileged helper for the network write path.
#
# Subcommands:
#   apply-iface <yaml-body> <timeout-s>
#       Backup the current /etc/netplan, drop the new YAML in place,
#       netplan generate + networkctl reload, schedule a systemd-run
#       rollback that fires after <timeout-s>. Idempotent — if a
#       previous pending apply is active, it is cancelled (rolled back)
#       before the new one starts.
#
#   confirm
#       Cancel the pending rollback timer + drop the backup. The
#       config is now permanent.
#
#   cancel
#       Cancel the pending rollback timer + immediately run the
#       rollback (restore from backup). Used for "Revert now".
#
#   status
#       Print JSON: { pending: bool, expires_at: <epoch>, backup_dir: <path> }
#       Doesn't need root (the daemon reads this directly), included
#       here so callers can pipe through one path.
#
#   rollback <backup-dir>
#       Internal — called by the timer. Restore /etc/netplan from the
#       backup dir + netplan generate + reload.
#
# Why not `netplan try` directly?
# `netplan try` is interactive — it reads stdin to accept the change
# and times out otherwise. From a non-interactive sudo path it's
# fragile (FIFO redirection, PID tracking across sudo boundaries).
# Re-implementing the try semantic with systemd-run gives us the same
# auto-revert guarantee with explicit state files and proper logging,
# all from a script we control.

set -euo pipefail

NETPLAN_DIR=/etc/netplan
PHONON_FILE_NAME=99-phonon-managed.yaml
PHONON_FILE="${NETPLAN_DIR}/${PHONON_FILE_NAME}"
STATE_DIR=/run/phonon-net
PENDING_FILE="${STATE_DIR}/pending.json"
BACKUP_ROOT=/var/lib/phonon/netplan-backups
ROLLBACK_UNIT=phonon-netplan-rollback
LOG=/var/log/phonon/net.log

mkdir -p "$(dirname "$LOG")" "$STATE_DIR" "$BACKUP_ROOT"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

# Drop any pending rollback timer. Quiet because the unit may not
# exist on a clean apply path — that's normal.
cancel_timer() {
    systemctl stop "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
    systemctl stop "${ROLLBACK_UNIT}.service" 2>/dev/null || true
    # `systemd-run` creates transient units that systemctl reset-failed
    # cleans up — without this they linger as "failed" in `systemctl list-units`.
    systemctl reset-failed "${ROLLBACK_UNIT}.timer" 2>/dev/null || true
    systemctl reset-failed "${ROLLBACK_UNIT}.service" 2>/dev/null || true
}

backup_netplan() {
    local stamp dst
    stamp="$(date +%Y%m%d-%H%M%S)"
    dst="${BACKUP_ROOT}/${stamp}"
    mkdir -p "$dst"
    if compgen -G "${NETPLAN_DIR}/*.yaml" > /dev/null; then
        cp -p "${NETPLAN_DIR}"/*.yaml "$dst/"
    fi
    echo "$dst"
}

write_pending() {
    local backup="$1"
    local expires_at="$2"
    cat > "$PENDING_FILE" <<EOF
{"backup_dir":"$backup","expires_at":$expires_at}
EOF
}

cmd_apply_iface() {
    local body="$1"
    local timeout="${2:-120}"
    if [ -z "$body" ]; then
        log "apply-iface: empty body — refusing"
        return 2
    fi
    case "$timeout" in
        ''|*[!0-9]*) log "apply-iface: bad timeout: $timeout"; return 2 ;;
    esac
    if [ "$timeout" -lt 30 ] || [ "$timeout" -gt 600 ]; then
        log "apply-iface: timeout out of range (30-600): $timeout"
        return 2
    fi

    # Roll back any previous pending apply BEFORE we save a new backup,
    # otherwise the new "backup" would capture the about-to-be-undone state.
    if [ -f "$PENDING_FILE" ]; then
        local prev_backup
        prev_backup="$(grep -oP '"backup_dir":"[^"]*"' "$PENDING_FILE" | cut -d'"' -f4)"
        log "apply-iface: prior pending detected, rolling back to $prev_backup first"
        cancel_timer
        if [ -n "$prev_backup" ] && [ -d "$prev_backup" ]; then
            cmd_rollback "$prev_backup"
        fi
    fi

    local backup
    backup="$(backup_netplan)"
    log "apply-iface: backup → $backup"

    # Write the new YAML atomically. tempfile + rename so the netplan
    # parser never sees half a file.
    local tmp
    tmp="$(mktemp --tmpdir="$NETPLAN_DIR" phonon-net.XXXXXX.yaml)"
    printf '%s' "$body" > "$tmp"
    chown root:root "$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$PHONON_FILE"
    log "apply-iface: wrote $PHONON_FILE ($(wc -l <"$PHONON_FILE") lines)"

    # netplan generate → /run/systemd/network → networkctl reload.
    # If generate fails, rollback right away so the OS isn't in a
    # half-configured state.
    if ! netplan generate 2>>"$LOG"; then
        log "apply-iface: netplan generate failed — rolling back immediately"
        cmd_rollback "$backup"
        return 3
    fi
    if ! networkctl reload 2>>"$LOG"; then
        log "apply-iface: networkctl reload failed — rolling back immediately"
        cmd_rollback "$backup"
        return 3
    fi
    log "apply-iface: applied OK, scheduling rollback in ${timeout}s"

    local expires_at=$(( $(date +%s) + timeout ))
    write_pending "$backup" "$expires_at"

    # systemd-run --on-active=Ns runs our rollback subcommand once,
    # after N seconds. The unit name is stable so confirm/cancel can
    # find it. Quiet=true keeps the spawn line out of the log.
    systemd-run \
        --unit="${ROLLBACK_UNIT}" \
        --on-active="${timeout}s" \
        --quiet \
        /usr/local/sbin/phonon-net rollback "$backup" \
        2>>"$LOG"

    return 0
}

cmd_confirm() {
    cancel_timer
    if [ -f "$PENDING_FILE" ]; then
        local backup
        backup="$(grep -oP '"backup_dir":"[^"]*"' "$PENDING_FILE" | cut -d'"' -f4)"
        if [ -n "$backup" ] && [ -d "$backup" ]; then
            rm -rf "$backup"
            log "confirm: dropped backup $backup"
        fi
        rm -f "$PENDING_FILE"
        log "confirm: change made permanent"
    else
        log "confirm: nothing pending"
    fi
}

cmd_cancel() {
    cancel_timer
    if [ -f "$PENDING_FILE" ]; then
        local backup
        backup="$(grep -oP '"backup_dir":"[^"]*"' "$PENDING_FILE" | cut -d'"' -f4)"
        if [ -n "$backup" ] && [ -d "$backup" ]; then
            cmd_rollback "$backup"
        else
            log "cancel: pending state has no valid backup — manual cleanup needed"
            rm -f "$PENDING_FILE"
        fi
    else
        log "cancel: nothing pending"
    fi
}

cmd_rollback() {
    local backup="$1"
    if [ -z "$backup" ] || [ ! -d "$backup" ]; then
        log "rollback: backup dir invalid: $backup"
        return 2
    fi
    log "rollback: restoring from $backup"
    # Remove our managed file first so it doesn't shadow the original.
    rm -f "$PHONON_FILE"
    # Restore every yaml that was in the backup — if the user had no
    # /etc/netplan/*.yaml before (fresh system), the backup dir is
    # empty and this is a no-op.
    if compgen -G "$backup/*.yaml" > /dev/null; then
        cp -p "$backup"/*.yaml "$NETPLAN_DIR/"
    fi
    netplan generate 2>>"$LOG" || log "rollback: netplan generate failed (continuing)"
    networkctl reload 2>>"$LOG" || log "rollback: networkctl reload failed (continuing)"
    rm -rf "$backup"
    rm -f "$PENDING_FILE"
    log "rollback: done"
}

cmd_status() {
    if [ -f "$PENDING_FILE" ]; then
        cat "$PENDING_FILE"
        echo
    else
        echo '{"pending":false}'
    fi
}

case "${1:-}" in
    apply-iface)  shift; cmd_apply_iface "${1:-}" "${2:-}" ;;
    confirm)      cmd_confirm ;;
    cancel)       cmd_cancel ;;
    rollback)     shift; cmd_rollback "${1:-}" ;;
    status)       cmd_status ;;
    *)
        echo "usage: $0 {apply-iface <yaml> <timeout-s> | confirm | cancel | rollback <backup-dir> | status}" >&2
        exit 1
        ;;
esac
