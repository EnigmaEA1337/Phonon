#!/usr/bin/env bash
# Phonon — disable PipeWire user services for every account except phonon.
#
# Why: on a headless Phonon Stage, only the `phonon` user runs the audio
# routing daemon. Other accounts (e.g. `manager` for SSH admin) have no
# legitimate need for PipeWire/WirePlumber/pipewire-pulse, but systemd-logind
# auto-starts them on every login. With both stacks alive, two
# wireplumber instances arbitrate the same ALSA hardware and fight over
# mixer state — symptom: random 0% hardware volumes after a reboot, sinks
# stuck on the wrong profile, ghost EPIPE storms in journal. See
# diagnostic in chat history (2026-05-10).
#
# This script:
#   * Masks pipewire / pipewire.socket / pipewire-pulse{,.socket} /
#     wireplumber for every non-phonon human user (UID 1000-65000) by
#     symlinking the unit name to /dev/null in their ~/.config/systemd/user.
#     Mask survives reinstalls because it lives in their home, not in
#     /usr or /etc.
#   * Stops the services right now if a session is currently running for
#     that user (via systemd-run --uid).
#   * Disables `loginctl linger` for those users so SSH disconnect tears
#     down their session instead of keeping pipewire processes alive.
#
# Idempotent — safe to re-run on every install.sh.
#
# Usage: sudo bash disable-rival-audio.sh [--phonon-user USER]
#
# Exits 0 on success, non-zero on failure.

set -euo pipefail

PHONON_USER="${PHONON_USER:-phonon}"

while [ $# -gt 0 ]; do
    case "$1" in
        --phonon-user)
            PHONON_USER="$2"
            shift 2
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root" >&2
    exit 1
fi

# Services we mask. The .socket units must be masked separately — masking
# only the .service leaves the socket-activated entry point intact and the
# service starts again on first connection attempt.
MASK_UNITS=(
    pipewire.socket
    pipewire.service
    pipewire-pulse.socket
    pipewire-pulse.service
    wireplumber.service
)

mask_for_user() {
    local user="$1"
    local uid="$2"
    local home
    home="$(getent passwd "$user" | cut -d: -f6)"
    if [ -z "$home" ] || [ ! -d "$home" ]; then
        echo "  skip $user: no usable home dir"
        return
    fi

    local user_units_dir="$home/.config/systemd/user"
    install -d -o "$user" -g "$user" -m 0755 "$user_units_dir"

    for unit in "${MASK_UNITS[@]}"; do
        local link="$user_units_dir/$unit"
        # Symlink to /dev/null = systemd treats unit as masked. Same effect
        # as `systemctl --user mask <unit>` but doesn't require an active
        # session for the user.
        ln -sfn /dev/null "$link"
        chown -h "$user:$user" "$link" 2>/dev/null || true
    done
    echo "  $user (uid $uid): masked ${#MASK_UNITS[@]} pipewire units"

    # Stop any currently-running services for this user. Best-effort — if
    # there's no active session, systemd-run will fail and we move on.
    if systemctl is-active "user@${uid}.service" --quiet 2>/dev/null; then
        local svc_args=()
        for unit in "${MASK_UNITS[@]}"; do
            svc_args+=("$unit")
        done
        systemd-run --uid="$user" --gid="$user" -p PAMName=login --pipe --wait -- \
            systemctl --user stop "${svc_args[@]}" 2>/dev/null || true
        echo "  $user: stop signal sent to running pipewire stack"
    fi

    # Disable lingering so the user's session ends with their last logout.
    # If nothing is keeping their user@.service alive, the audio processes
    # can't outlive the SSH connection.
    loginctl disable-linger "$user" 2>/dev/null || true
}

echo "Phonon — disabling rival PipeWire user stacks (keeping ${PHONON_USER} only)..."

found_any=false
while IFS=: read -r user _ uid _ _ _ _; do
    # Real users only (UID 1000-65000), skip the phonon daemon user.
    if [ "$uid" -lt 1000 ] || [ "$uid" -ge 65000 ]; then
        continue
    fi
    if [ "$user" = "$PHONON_USER" ]; then
        continue
    fi
    mask_for_user "$user" "$uid"
    found_any=true
done < <(getent passwd)

if [ "$found_any" = false ]; then
    echo "  no non-phonon human users found — nothing to do"
fi

# Make sure phonon's stack is NOT masked. install.sh masks/unmasks based
# on its own logic; we just guarantee we didn't accidentally mask phonon
# above. (Won't happen given the loop guard, but cheap to assert.)
PHONON_HOME="$(getent passwd "$PHONON_USER" | cut -d: -f6)"
if [ -n "$PHONON_HOME" ] && [ -d "$PHONON_HOME/.config/systemd/user" ]; then
    for unit in "${MASK_UNITS[@]}"; do
        link="$PHONON_HOME/.config/systemd/user/$unit"
        if [ -L "$link" ] && [ "$(readlink "$link")" = "/dev/null" ]; then
            rm -f "$link"
            echo "  phonon: removed accidental mask on $unit"
        fi
    done
fi

echo "Done. New SSH logins for non-phonon users will no longer auto-start PipeWire."
echo "(Currently-active manager sessions retain their pipewire until logout.)"
