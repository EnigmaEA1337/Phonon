#!/usr/bin/env bash
# Phonon — Bluetooth factory reset, triggered by /bluetooth/factory-reset.
# Runs under root via NOPASSWD sudoers grant on /usr/local/sbin/phonon-bt-reset.
#
# Stops the BT stack, wipes every paired-device record and discovery
# cache from /var/lib/bluetooth/<adapter>/, keeps the adapter's
# settings file, restarts the stack. Result: BlueZ comes back with
# zero paired devices and an empty discovery cache, as if the host
# was just installed.

set -euo pipefail

LOG=/var/log/phonon/bt-reset.log
LOCK=/run/phonon-bt-reset.lock
mkdir -p "$(dirname "$LOG")"
exec 200>"$LOCK"
if ! flock -n 200; then
    echo "[$(date -Iseconds)] bt-reset already in progress — bailing out" >&2
    exit 1
fi
exec > >(tee -a "$LOG") 2>&1

echo "============================================================"
echo "phonon bt-reset started at $(date -Iseconds)"
echo "============================================================"

# Stop the stack BEFORE deleting state — bluetoothd holds open fds
# on its bond directories. Order matters: bluealsa first (the
# A2DP-over-DBus consumer), then phonon-bt-agent (our pairing agent),
# then bluetoothd itself.
for svc in bluealsa phonon-bt-agent bluetooth; do
    if systemctl is-active --quiet "$svc"; then
        echo "stopping $svc..."
        systemctl stop "$svc"
    fi
done
sleep 1

# Wipe paired-device dirs (named by MAC, e.g. 40:C1:F6:FC:5A:02/)
# and the discovery cache subdir, keeping each adapter's settings
# file intact so the adapter's name/discoverability/etc. survive.
removed=0
for adapter_dir in /var/lib/bluetooth/*/; do
    [ -d "$adapter_dir" ] || continue
    echo "adapter: $adapter_dir"
    while IFS= read -r -d '' sub; do
        echo "  removing $sub"
        rm -rf "$sub"
        removed=$((removed + 1))
    done < <(find "$adapter_dir" -mindepth 1 -maxdepth 1 -type d -print0)
done
echo "removed $removed subdirectories"

# Restart in dependency order — bluetoothd first so its DBus is up
# when bluealsa connects, then bluealsa, then our pairing agent.
for svc in bluetooth bluealsa phonon-bt-agent; do
    if systemctl is-enabled --quiet "$svc" 2>/dev/null; then
        echo "starting $svc..."
        systemctl start "$svc" || echo "  (failed to start $svc — continuing)"
    fi
done

echo "============================================================"
echo "phonon bt-reset finished at $(date -Iseconds)"
echo "============================================================"
