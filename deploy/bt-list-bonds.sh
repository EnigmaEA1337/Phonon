#!/usr/bin/env bash
# Phonon — emit a JSON list of every device bonded on disk under
# /var/lib/bluetooth/, even if BlueZ has unloaded its D-Bus object
# (typical when a paired BT speaker goes idle for a while).
#
# Runs under root via NOPASSWD sudo on /usr/local/sbin/phonon-bt-list-bonds.
# Output goes to stdout, one device per JSON object — easy to parse from
# the daemon and merge with the live D-Bus device list.

set -euo pipefail

emit() {
    local adapter="$1" device="$2" info="$3"
    local name="" trusted="false" blocked="false" class=""
    while IFS='=' read -r key value; do
        case "$key" in
            Name)              name="$value" ;;
            Trusted)           trusted="$value" ;;
            Blocked)           blocked="$value" ;;
            Class)             class="$value" ;;
        esac
    done < <(grep -hE '^(Name|Trusted|Blocked|Class)=' "$info" 2>/dev/null || true)
    # JSON-escape minimal — names can contain quotes/backslashes
    name="${name//\\/\\\\}"; name="${name//\"/\\\"}"
    printf '{"adapter":"%s","address":"%s","name":"%s","trusted":%s,"blocked":%s,"class":"%s"}\n' \
        "$adapter" "$device" "$name" "$trusted" "$blocked" "$class"
}

for adapter_dir in /var/lib/bluetooth/*/; do
    [ -d "$adapter_dir" ] || continue
    adapter="${adapter_dir%/}"
    adapter="${adapter##*/}"
    while IFS= read -r -d '' device_dir; do
        device="${device_dir%/}"
        device="${device##*/}"
        # Only directories whose name looks like a MAC address (6 colon-
        # separated hex pairs) — the 'cache' subdir is excluded by this.
        if [[ "$device" =~ ^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$ ]] && [ -f "$device_dir/info" ]; then
            emit "$adapter" "$device" "$device_dir/info"
        fi
    done < <(find "$adapter_dir" -mindepth 1 -maxdepth 1 -type d -print0)
done
