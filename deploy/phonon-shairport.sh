#!/bin/sh
# Wrapper that picks the right shairport-sync binary based on the
# `airplay-version` line in the Phonon AirPlay plugin conf file.
#
# Why this exists: shairport-sync 4.3.7 built with --with-airplay-2
# does read `airplay-version = 1;` from the conf, but still starts in
# AP2 mode regardless of the value — apparent upstream bug. The apt-
# packaged binary at /usr/bin/shairport-sync is built without
# --with-airplay-2 and operates strictly as AP1. So the only reliable
# way to switch the operating mode is to swap the actual binary.
#
# Mapping:
#   conf says `airplay-version = 2;`  → /usr/local/bin/shairport-sync (AP2-capable build)
#   conf says `airplay-version = 1;`  → /usr/bin/shairport-sync       (apt AP1-only)
#   no AP2 build present              → /usr/bin/shairport-sync       (AP1 forced regardless)
#   no apt binary present             → /usr/local/bin/shairport-sync (best effort)
#
# Args are forwarded verbatim (the systemd unit passes -c <conf> -o pa).

set -e

CONF=""
NEXT_IS_CONF=0
for arg in "$@"; do
    if [ "$NEXT_IS_CONF" = "1" ]; then
        CONF="$arg"
        NEXT_IS_CONF=0
        continue
    fi
    case "$arg" in
        -c) NEXT_IS_CONF=1 ;;
        --configfile=*) CONF="${arg#*=}" ;;
    esac
done

# Default to AP1 unless the conf positively asks for AP2 — keeps the
# wrapper safe when the conf doesn't exist yet (first boot before the
# plugin renders it).
WANT_AP2=0
if [ -n "$CONF" ] && [ -r "$CONF" ]; then
    if grep -qE '^[[:space:]]*airplay-version[[:space:]]*=[[:space:]]*2' "$CONF" 2>/dev/null; then
        WANT_AP2=1
    fi
fi

AP2_BIN=/usr/local/bin/shairport-sync
AP1_BIN=/usr/bin/shairport-sync

if [ "$WANT_AP2" = "1" ] && [ -x "$AP2_BIN" ]; then
    exec "$AP2_BIN" "$@"
fi
if [ -x "$AP1_BIN" ]; then
    exec "$AP1_BIN" "$@"
fi
# Neither path was viable in its preferred role — fall back to the
# AP2 build (which can also act as AP2-default if the user wanted v1
# we can't honour without the apt binary, but at least the daemon
# stays alive).
exec "$AP2_BIN" "$@"
