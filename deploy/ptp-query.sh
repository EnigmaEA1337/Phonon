#!/usr/bin/env bash
# Phonon — wrapper around pmc + writer for /etc/default/phonon-ptp.
# Runs under root via NOPASSWD sudo on /usr/local/sbin/phonon-ptp-query.
#
# pmc treats its trailing 'GET <SET>' as a single quoted argument; the
# Python daemon's asyncio.create_subprocess_exec splits that into two
# argv slots and pmc bails with 'bad command'. Sudoers can match the
# quoted-arg form in theory, but sudo's argv-vs-sudoers tokenizer
# doesn't reliably accept the quoted token from a Python invocation.
# This wrapper hard-codes the two read-only PTP queries we need, takes
# only one of two whitelisted command names as $1, and is granted
# NOPASSWD-sudo with no args. Daemon-safe, sudoers-clean.
#
# set-iface <name>: write PTP_IFACE to /etc/default/phonon-ptp so the
# next phonon-ptp4l.service start binds to that interface. Without
# this writer the Settings.ptp.interface UI field was cosmetic — the
# unit's ExecStartPre auto-picked whichever iface was up first.

set -euo pipefail

case "${1:-}" in
    port)    exec /usr/sbin/pmc -u -b 0 "GET PORT_DATA_SET" ;;
    current) exec /usr/sbin/pmc -u -b 0 "GET CURRENT_DATA_SET" ;;
    parent)  exec /usr/sbin/pmc -u -b 0 "GET PARENT_DATA_SET" ;;
    set-iface)
        iface="${2:-}"
        # Validate: 1-15 chars, alnum + ._- (Linux iface name rules).
        # Empty = clear (auto-pick on next start), which is also a
        # valid op so we accept it.
        if [ -n "$iface" ]; then
            if [ "${#iface}" -gt 15 ] || ! [[ "$iface" =~ ^[A-Za-z0-9._-]+$ ]]; then
                echo "set-iface: invalid iface name: $iface" >&2
                exit 2
            fi
        fi
        # Atomic rewrite of /etc/default/phonon-ptp. The file is
        # sourced as a sh fragment by the systemd unit's
        # EnvironmentFile, so the PTP_IFACE= line is the only thing
        # that matters; we preserve the rest of the file's defaults
        # by regenerating the canonical body.
        tmp="$(mktemp --tmpdir=/etc/default phonon-ptp.XXXXXX)"
        # The variable read by the unit's ExecStartPre is
        # PHONON_PTP_IFACE_OVERRIDE — distinct from the runtime
        # PTP_IFACE in /run/phonon-ptp.env so a stale runtime file
        # from the previous start doesn't shadow this override.
        cat > "$tmp" <<EOF
# Managed by phonon-stage. Set via UI -> CLOCKWORLD -> PTP iface picker.
# Empty value = ExecStartPre auto-picks the first UP non-lo iface.
PHONON_PTP_IFACE_OVERRIDE=${iface}
# PTP_MODE_FLAG is auto-managed by the renderer too.
PTP_MODE_FLAG=
EOF
        chmod 0644 "$tmp"
        mv -f "$tmp" /etc/default/phonon-ptp
        echo "set-iface: PHONON_PTP_IFACE_OVERRIDE=${iface:-<auto>} written to /etc/default/phonon-ptp"
        ;;
    *)
        echo "usage: $0 {port|current|parent|set-iface <iface>}" >&2
        exit 2
        ;;
esac
