#!/usr/bin/env bash
# Phonon — wrapper around pmc to bypass the sudoers/quoting quirk.
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

set -euo pipefail

case "${1:-}" in
    port)    exec /usr/sbin/pmc -u -b 0 "GET PORT_DATA_SET" ;;
    current) exec /usr/sbin/pmc -u -b 0 "GET CURRENT_DATA_SET" ;;
    parent)  exec /usr/sbin/pmc -u -b 0 "GET PARENT_DATA_SET" ;;
    *)       echo "usage: $0 {port|current|parent}" >&2; exit 2 ;;
esac
