#!/usr/bin/env bash
# build-shairport-ap2 — build shairport-sync (with AirPlay 2 support)
# and its nqptp companion from upstream sources.
#
# Why: the Ubuntu apt package of shairport-sync is built WITHOUT
# `--with-airplay-2`, so it can only act as an AirPlay 1 receiver.
# AP2 unlocks lossless ALAC 24/48 + multi-room sync via nqptp.
#
# Idempotent: re-runs short-circuit if the binaries are already
# present AND the shairport-sync `-V` feature string mentions
# `airplay-2`. Re-running after a `--force` arg triggers a full
# rebuild (used for upgrades).
#
# Called from install.sh (Step 8) on hosts where the AirPlay v1
# plugin's settings include airplay_version=2.

set -euo pipefail

FORCE="${1:-}"
LOG=/var/log/phonon/build-shairport-ap2.log
WORK=/var/lib/phonon/build/shairport-ap2

NQPTP_REPO=https://github.com/mikebrady/nqptp.git
SHAIRPORT_REPO=https://github.com/mikebrady/shairport-sync.git
# Pin to the upstream stable tags that match the AP2 feature set we
# test against. Bump these together; mismatched (e.g. fresh
# shairport-sync against an old nqptp) is known to fail handshake.
NQPTP_TAG=1.2.4
SHAIRPORT_TAG=4.3.7

mkdir -p "$(dirname "$LOG")" "$WORK"

log() {
    echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

short_circuit_ok() {
    # Already built + AP2 in the feature string + nqptp present.
    # nqptp's Makefile installs to /usr/local/bin/ (not sbin); accept
    # either location for forward-compat. Feature string can read
    # `AirPlay2` (capital) on shairport >= 4.3 or `airplay-2`
    # (dashed lowercase) on older builds — accept either.
    [ -x /usr/local/bin/shairport-sync ] || return 1
    [ -x /usr/local/bin/nqptp ] || [ -x /usr/local/sbin/nqptp ] || return 1
    /usr/local/bin/shairport-sync -V 2>&1 | grep -qiE "airplay[-]?2" || return 1
    return 0
}

if [ "$FORCE" != "--force" ] && short_circuit_ok; then
    log "shairport-sync AP2 + nqptp already built — skipping (use --force to rebuild)"
    exit 0
fi

log "==== build-shairport-ap2 started ===="
log "FORCE=${FORCE:-no}  NQPTP=${NQPTP_TAG}  SHAIRPORT=${SHAIRPORT_TAG}"

# ─── apt build dependencies ─────────────────────────────────────
# Pin to the minimum set shairport-sync 4.3.x AP2 needs. Apt
# resolves duplicates against installed packages — installing
# again is a no-op.
DEBIAN_FRONTEND=noninteractive apt-get update >>"$LOG" 2>&1 || true
DEBIAN_FRONTEND=noninteractive apt-get install -y >>"$LOG" 2>&1 \
    build-essential autoconf automake libtool pkg-config xxd git \
    libpopt-dev libconfig-dev libssl-dev libsoxr-dev \
    libavahi-client-dev libplist-dev libsodium-dev libgcrypt-dev \
    libavcodec-dev libavformat-dev libavutil-dev libswresample-dev \
    uuid-dev libdaemon-dev libffi-dev \
    libasound2-dev libpulse-dev libmosquitto-dev libglib2.0-dev
log "apt build deps installed"

# ─── nqptp ──────────────────────────────────────────────────────
cd "$WORK"
if [ -d nqptp/.git ]; then
    log "nqptp repo present — fetching"
    (cd nqptp && git fetch --tags --quiet) >>"$LOG" 2>&1
else
    log "cloning nqptp"
    git clone --depth=1 --branch="${NQPTP_TAG}" "${NQPTP_REPO}" nqptp >>"$LOG" 2>&1 \
        || git clone "${NQPTP_REPO}" nqptp >>"$LOG" 2>&1
fi
cd nqptp
git checkout "${NQPTP_TAG}" >>"$LOG" 2>&1 || git checkout main >>"$LOG" 2>&1
autoreconf -fi >>"$LOG" 2>&1
./configure --with-systemd-startup >>"$LOG" 2>&1
make -j"$(nproc)" >>"$LOG" 2>&1
make install >>"$LOG" 2>&1
# nqptp's Makefile installs to /usr/local/bin (not sbin). Log
# whichever path actually has the binary so the message is honest.
if [ -x /usr/local/bin/nqptp ]; then
    log "nqptp installed → /usr/local/bin/nqptp"
elif [ -x /usr/local/sbin/nqptp ]; then
    log "nqptp installed → /usr/local/sbin/nqptp"
else
    log "ERROR: nqptp build claimed success but binary not found"
    exit 4
fi

# ─── shairport-sync ─────────────────────────────────────────────
cd "$WORK"
if [ -d shairport-sync/.git ]; then
    log "shairport-sync repo present — fetching"
    (cd shairport-sync && git fetch --tags --quiet) >>"$LOG" 2>&1
else
    log "cloning shairport-sync"
    git clone --depth=1 --branch="${SHAIRPORT_TAG}" "${SHAIRPORT_REPO}" shairport-sync >>"$LOG" 2>&1 \
        || git clone "${SHAIRPORT_REPO}" shairport-sync >>"$LOG" 2>&1
fi
cd shairport-sync
git checkout "${SHAIRPORT_TAG}" >>"$LOG" 2>&1 || git checkout master >>"$LOG" 2>&1
autoreconf -fi >>"$LOG" 2>&1
# Feature flags: same as Ubuntu's apt build + --with-airplay-2 +
# --with-pa (PulseAudio backend — what our pipewire-pulse compat
# layer pretends to be). We KEEP /etc/shairport-sync.conf as the
# default config dir even though the phonon plugin writes to
# /var/lib/phonon/plugins/airplay-v1/ — the systemd drop-in below
# passes -c explicitly so this doesn't matter at runtime.
./configure \
    --sysconfdir=/etc \
    --with-alsa \
    --with-pa \
    --with-soxr \
    --with-avahi \
    --with-ssl=openssl \
    --with-airplay-2 \
    --with-metadata \
    --with-dbus-interface \
    --with-mpris-interface \
    >>"$LOG" 2>&1
# --with-systemd dropped: it tries to `install -d $(UNITDIR)` at
# `make install` time, and without an explicit
# --with-systemd-unit-dir the variable expands to empty → install
# barfs ("install with -d requires at least one argument"). We
# install our own /etc/systemd/system/shairport-sync.service.d/
# drop-in via install.sh, so the upstream-installed unit isn't
# needed anyway.
make -j"$(nproc)" >>"$LOG" 2>&1
make install >>"$LOG" 2>&1
log "shairport-sync AP2 installed → /usr/local/bin/shairport-sync"

# Sanity check: the resulting binary must report `airplay-2` in
# its `-V` feature string. If it doesn't, the build silently
# fell back to AP1-only (missing libplist / libsodium / etc.) and
# we want install.sh to fail loudly here.
if ! /usr/local/bin/shairport-sync -V 2>&1 | grep -qiE "airplay[-]?2"; then
    log "ERROR: build completed but binary doesn't report airplay-2 — see log above"
    exit 3
fi
log "shairport-sync features: $(/usr/local/bin/shairport-sync -V 2>&1 | head -1)"
log "==== build-shairport-ap2 finished OK ===="
