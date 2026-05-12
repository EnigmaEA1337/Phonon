#!/usr/bin/env bash
# Phonon Stage Agent — install script (multi-arch, idempotent)
# Usage:
#   sudo bash install.sh                          # default: stage-only
#   sudo bash install.sh --role=stage-only        # explicit stage
#   sudo bash install.sh --role=controller-only   # controller only (future)
set -euo pipefail

# ── Constants ───────────���────────────────────────────────────────────────

PHONON_USER="phonon"
PHONON_GROUP="phonon"
INSTALL_DIR="/opt/phonon/stage"
VENV_DIR="${INSTALL_DIR}/.venv"
CONFIG_DIR="/etc/phonon"
DATA_DIR="/var/lib/phonon"
LOG_DIR="/var/log/phonon"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
STAGE_SRC="${REPO_ROOT}/stage"

# ── Argument parsing ─────────────────────────────────────────────────────

ROLE="stage-only"

for arg in "$@"; do
    case "$arg" in
        --role=*) ROLE="${arg#*=}" ;;
        --help|-h)
            echo "Usage: sudo bash install.sh [--role=stage-only|controller-only]"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown argument: $arg"
            exit 1
            ;;
    esac
done

# ── Checks ────────────���─────────────────────────────���────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] This script must be run as root (sudo)"
    exit 1
fi

# ── Step 1/7: Detect architecture and distro ─────────────────────────────

echo "[1/11] Detecting platform..."

ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  PLATFORM="x86_64" ;;
    aarch64) PLATFORM="arm64" ;;
    *)
        echo "[ERROR] Unsupported architecture: $ARCH"
        exit 1
        ;;
esac

if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO="${ID:-unknown}"
    DISTRO_VERSION="${VERSION_ID:-unknown}"
else
    echo "[ERROR] Cannot detect distribution (/etc/os-release missing)"
    exit 1
fi

echo "  Platform: ${PLATFORM} | Distro: ${DISTRO} ${DISTRO_VERSION}"

# ── Step 2/7: Install system dependencies ────────���───────────────────────

echo "[2/11] Installing system dependencies..."

apt-get update -qq
# Don't swallow install errors — a missing package here (typically
# linuxptp on a stale package list, or pipewire on a non-Bookworm
# distro) used to fail silently and surface much later as 'Job for
# phonon-ptp4l.service failed' with no clue why.
if ! apt-get install -y -qq \
    python3-venv \
    python3-dev \
    libdbus-1-dev \
    pkg-config \
    avahi-daemon \
    bluez \
    bluez-tools \
    alsa-utils \
    pipewire \
    pipewire-alsa \
    pipewire-pulse \
    wireplumber \
    pulseaudio-utils \
    linuxptp \
    shairport-sync ; then
    echo "  ERROR: apt-get install failed — fix the network or repo issue and rerun" >&2
    exit 1
fi

# spotifyd — Spotify Connect daemon. Built on top of librespot but
# unlike librespot it ships prebuilt binaries for every release.
# Download the `-full` flavour (alsa + pulseaudio + dbus_mpris) for
# the host's arch and drop the binary at /usr/bin/spotifyd so the
# unit's ConditionPathExists passes. Skip if already present.
if ! command -v spotifyd >/dev/null 2>&1; then
    echo "  Installing spotifyd (Spotify plugin)..."
    case "${PLATFORM}" in
        x86_64)  SPD_ARCH="x86_64" ;;
        arm64)   SPD_ARCH="aarch64" ;;
        *)       SPD_ARCH="" ;;
    esac
    if [ -n "${SPD_ARCH}" ]; then
        SPD_URL="$(curl -fsSL https://api.github.com/repos/Spotifyd/spotifyd/releases/latest \
            | grep '"browser_download_url".*spotifyd-linux-'"${SPD_ARCH}"'-full\.tar\.gz' \
            | head -1 \
            | cut -d'"' -f4 || true)"
        if [ -n "${SPD_URL}" ]; then
            echo "  Downloading ${SPD_URL}..."
            if curl -fsSL "${SPD_URL}" | tar -xz -C /tmp/ spotifyd 2>/dev/null \
               && mv /tmp/spotifyd /usr/bin/spotifyd \
               && chmod +x /usr/bin/spotifyd; then
                echo "  spotifyd binary installed to /usr/bin/spotifyd"
            else
                echo "  WARN: download or extract failed — Spotify plugin will stay inert"
            fi
        else
            echo "  WARN: no GitHub release found for arch ${SPD_ARCH}"
        fi
    else
        echo "  WARN: unsupported arch ${PLATFORM} for spotifyd binary download"
    fi
fi

# Verify the binaries the rest of install.sh expects to find. Catches
# the rare case where a package was 'installed' but its binary lives
# under a different path (e.g. a sysroot mismatch on cross-builds).
for bin in /usr/sbin/ptp4l /usr/sbin/phc2sys /usr/bin/wpctl /usr/bin/pw-link /usr/bin/pactl /usr/bin/amixer /usr/bin/aplay /usr/sbin/alsactl; do
    if [ ! -x "${bin}" ]; then
        echo "  ERROR: expected binary ${bin} missing after apt install" >&2
        exit 1
    fi
done

echo "  System packages OK"

# ── Step 2bis: Install lowlatency kernel (x86_64 + apt only) ────────────
# Raspberry Pi OS uses its own kernel image (linux-image-rpi-*) — no
# lowlatency variant. Skip silently when the package isn't available.
#
# The lowlatency kernel switches to CONFIG_PREEMPT + HZ=1000, which on
# our scope cuts audio xrun risk roughly in half. Phonon runs fine on
# the generic kernel — the install just won't be optimal until a
# reboot picks up the new image. Marked idempotent (no-op if already
# on lowlatency).

NEEDS_REBOOT_FOR_LOWLATENCY=false

if apt-cache show linux-lowlatency >/dev/null 2>&1; then
    if uname -r | grep -q "lowlatency"; then
        echo "  Lowlatency kernel already running ($(uname -r))"
    else
        echo "  Installing linux-lowlatency kernel..."
        if apt-get install -y -qq linux-lowlatency; then
            NEEDS_REBOOT_FOR_LOWLATENCY=true
            echo "  Lowlatency kernel installed — reboot needed after install.sh"
        else
            echo "  WARN: linux-lowlatency install failed, keeping generic kernel"
        fi
    fi
else
    echo "  No linux-lowlatency package on this distro — skipping"
fi

# ── Step 2ter: Install LADSPA + LSP plugins (x86_64 only) ────────────────
# DSP plugin inserts (filter-chain → LADSPA → LSP comp_delay_stereo etc.)
# are scoped to x86_64 Stages in v1 — see CLAUDE.md memory project_plugins_scope.
# Pis stay plugin-free: Cortex-A53 doesn't have the CPU headroom alongside
# null-sinks + loopbacks + bluealsa bridges. analyseplugin (LADSPA SDK)
# is what the Stage shells out to for runtime introspection of plugin
# control ports.

if [ "${PLATFORM}" = "x86_64" ]; then
    echo "  Installing LSP LADSPA plugins + analyseplugin (x86_64)..."
    if apt-get install -y -qq lsp-plugins-ladspa ladspa-sdk; then
        echo "  LADSPA plugins OK"
    else
        echo "  WARN: lsp-plugins-ladspa install failed — DSP inserts will be unavailable"
    fi
else
    echo "  Skipping LSP plugins on ${PLATFORM} (Pi: plugin inserts disabled in v1)"
fi

# ── Step 3/7: Create phonon user ────────────────────────────────────────

echo "[3/11] Creating phonon user..."

if id -u "${PHONON_USER}" &>/dev/null; then
    echo "  User ${PHONON_USER} already exists"
    # Existing user might have been created with -d /nonexistent (old
    # install.sh behaviour). WirePlumber refuses to write its state
    # under /nonexistent, the daemon fails subtly. Force-correct.
    if [ "$(getent passwd "${PHONON_USER}" | cut -d: -f6)" != "${DATA_DIR}" ]; then
        # usermod fails if the user has live processes (its pipewire
        # session via linger). Stop the user instance first.
        loginctl disable-linger "${PHONON_USER}" 2>/dev/null || true
        systemctl stop "user@$(id -u "${PHONON_USER}").service" 2>/dev/null || true
        sleep 1
        usermod -d "${DATA_DIR}" "${PHONON_USER}" || true
        echo "  Corrected ${PHONON_USER} home dir to ${DATA_DIR}"
    fi
else
    # -d "${DATA_DIR}" --no-create-home: record home in /etc/passwd as
    # /var/lib/phonon (so HOME=$DATA_DIR for the user's systemd session
    # and WirePlumber state lands there), but don't pre-populate
    # /var/lib/phonon with skeleton files — step 4 creates that dir
    # with the right perms.
    useradd -r -s /usr/sbin/nologin -d "${DATA_DIR}" --no-create-home "${PHONON_USER}"
    echo "  User ${PHONON_USER} created (home=${DATA_DIR})"
fi

# Add to audio + bluetooth groups, plus systemd-journal so the
# /ptp/status and /system/services endpoints can read the system
# journal (ptp4l, phonon-bt-agent…) — without it journalctl returns
# 'No journal files were opened due to insufficient permissions',
# which surfaces as the PTP role permanently stuck on 'unknown'.
usermod -aG audio "${PHONON_USER}" 2>/dev/null || true
usermod -aG bluetooth "${PHONON_USER}" 2>/dev/null || true
usermod -aG systemd-journal "${PHONON_USER}" 2>/dev/null || true
echo "  User in audio + bluetooth + systemd-journal groups"

# Install D-Bus policy for BlueZ access
if [ -f "${SCRIPT_DIR}/dbus/phonon-bluetooth.conf" ]; then
    cp "${SCRIPT_DIR}/dbus/phonon-bluetooth.conf" /etc/dbus-1/system.d/
    echo "  D-Bus BlueZ policy installed"
fi

# Install udev rules for stable USB device naming (UD100, DG60, X-Fi, ...)
# These are placeholders until the user maps real MAC/serials to symlinks;
# safe to install — non-matching rules are inert.
if [ -f "${SCRIPT_DIR}/udev/99-phonon.rules" ]; then
    cp "${SCRIPT_DIR}/udev/99-phonon.rules" /etc/udev/rules.d/
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger 2>/dev/null || true
    echo "  udev rules installed (edit /etc/udev/rules.d/99-phonon.rules with real IDs)"
fi

# ── Step 4/7: Create directories ────────────────────────────────────────

echo "[4/11] Creating directories..."

mkdir -p "${CONFIG_DIR}" "${DATA_DIR}" "${LOG_DIR}" "${INSTALL_DIR}"
chown "${PHONON_USER}:${PHONON_GROUP}" "${DATA_DIR}" "${LOG_DIR}"
chmod 0750 "${DATA_DIR}"
chmod 0755 "${LOG_DIR}" "${CONFIG_DIR}"

echo "  Directories OK"

# -- Pi Tuning (Stage 1 optimizations) ----------------------------------------

if [ "${PLATFORM}" = "arm64" ]; then
    echo "[4b] Applying Pi tuning..."

    # Disable useless services
    for svc in triggerhappy ModemManager hciuart keyboard-setup console-setup; do
        systemctl disable --now "${svc}.service" 2>/dev/null || true
    done
    # Disable wpa_supplicant only if no WiFi interface is active
    if ! ip link show wlan0 &>/dev/null; then
        systemctl disable --now wpa_supplicant.service 2>/dev/null || true
    fi
    echo "    Useless services disabled"

    # Disable onboard BT + WiFi
    for overlay in disable-bt disable-wifi; do
        if ! grep -q "dtoverlay=${overlay}" /boot/firmware/config.txt 2>/dev/null; then
            echo "dtoverlay=${overlay}" >> /boot/firmware/config.txt
        fi
    done
    echo "    Onboard BT + WiFi disabled (next reboot)"

    # CPU governor performance
    echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor > /dev/null 2>&1 || true
    cat > /etc/systemd/system/cpu-performance.service <<'CPUSVC'
[Unit]
Description=Set CPU governor to performance
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c 'echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
CPUSVC
    systemctl daemon-reload
    systemctl enable cpu-performance.service --quiet 2>/dev/null
    echo "    CPU governor: performance"

    # GPU memory 32MB
    if grep -q "^gpu_mem=" /boot/firmware/config.txt 2>/dev/null; then
        sed -i 's/^gpu_mem=.*/gpu_mem=32/' /boot/firmware/config.txt
    else
        echo "gpu_mem=32" >> /boot/firmware/config.txt
    fi
    echo "    GPU memory: 32MB (next reboot)"

    # fstab: nodiratime + commit=120
    if ! grep -q "nodiratime" /etc/fstab; then
        sed -i 's/defaults,noatime/defaults,noatime,nodiratime,commit=120/' /etc/fstab
        echo "    fstab: added nodiratime,commit=120"
    fi

    # Disable swap
    swapoff -a 2>/dev/null || true
    rm -f /var/swap
    systemctl disable dphys-swapfile 2>/dev/null || true
    echo "    Swap disabled"

    # Fast boot
    systemctl disable systemd-networkd-wait-online.service 2>/dev/null || true
    systemctl disable apt-daily.service apt-daily.timer 2>/dev/null || true
    systemctl disable apt-daily-upgrade.service apt-daily-upgrade.timer 2>/dev/null || true
    echo "    Fast boot enabled"

    echo "  Pi tuning applied"
else
    echo "[4b] Skipping Pi tuning (not arm64)"
fi

# ── Step 5/7: Python venv + install package ──────────────────────────────

echo "[5/11] Setting up Python venv and installing phonon-stage..."

if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    echo "  venv created"
else
    echo "  venv already exists"
fi

"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
# --force-reinstall --no-deps ensures source-only changes (no version
# bump) are picked up. pip otherwise says 'Requirement already satisfied'
# and skips the rebuild — leaving the venv with stale site-packages
# even after `git pull`. --no-deps skips re-pulling pinned deps.
"${VENV_DIR}/bin/pip" install --force-reinstall --no-deps "${STAGE_SRC}" --quiet
# But we DO want any newly-added dep (e.g. added in pyproject.toml) to
# be installed — second pass with deps but no force-reinstall is cheap.
"${VENV_DIR}/bin/pip" install "${STAGE_SRC}" --quiet

echo "  phonon-stage installed"

# ── Step 6/7: Generate config if absent ──────────────────────────────────

echo "[6/11] Generating config..."

if [ ! -f "${CONFIG_DIR}/stage.yaml" ]; then
    # Detect primary IP address
    BIND_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' || echo "127.0.0.1")

    cat > "${CONFIG_DIR}/stage.yaml" <<YAML
# Phonon Stage Agent configuration
# Generated by install.sh on $(date -Iseconds)
bind_address: "${BIND_IP}"
port: 8401
log_level: "INFO"
YAML

    chown "${PHONON_USER}:${PHONON_GROUP}" "${CONFIG_DIR}/stage.yaml"
    chmod 0640 "${CONFIG_DIR}/stage.yaml"
    echo "  Config generated (bind_address: ${BIND_IP})"
else
    echo "  Config already exists, not overwriting"
fi

# -- Step 7/10: Disable old system service if present -------------------------

echo "[7/11] Cleaning up old system service..."

if systemctl is-enabled phonon-stage.service 2>/dev/null; then
    systemctl stop phonon-stage.service 2>/dev/null
    systemctl disable phonon-stage.service 2>/dev/null
    echo "  Old system service disabled"
else
    echo "  No old system service found"
fi

# -- Step 8/10: Install BT auxiliary services ---------------------------------

echo "[8/11] Installing BT auxiliary services..."

# Capture user-set enable/disable state of BT helpers BEFORE we touch
# the unit files. We read it now (returns 'not-found' on a fresh
# machine, 'enabled' or 'disabled' on a previously-installed one) so a
# subsequent re-run of install.sh respects `systemctl disable
# phonon-bt-agent` set manually by a user who's running DG60s instead
# of BlueZ. cat-overwriting the .service file does NOT change the
# enable symlinks, so capturing here is equivalent to capturing right
# before the enable step — but doing it now keeps the intent obvious.
BT_AGENT_PREVSTATE=$(systemctl is-enabled phonon-bt-agent.service 2>/dev/null || echo "not-found")
BT_UNBLOCK_PREVSTATE=$(systemctl is-enabled phonon-bt-unblock.service 2>/dev/null || echo "not-found")

# Install python3-dbus + python3-gi for bt-agent
apt-get install -y -qq python3-dbus python3-gi bluez-alsa-utils 2>/dev/null || true

# Disable bluealsa-aplay (Phonon manages routing itself)
systemctl disable bluealsa-aplay 2>/dev/null || true
systemctl stop bluealsa-aplay 2>/dev/null || true

# Disable the system-wide shairport-sync unit shipped by the package.
# Phonon's AirPlay v1 plugin controls a user-instance unit instead
# (runs under the phonon UID so it shares the pipewire-pulse session
# we use for routing). Leaving the system unit enabled would race for
# the same network ports and announce a second AirPlay receiver on
# the network — confusing and wasteful.
systemctl disable shairport-sync 2>/dev/null || true
systemctl stop shairport-sync 2>/dev/null || true

# BlueALSA D-Bus policy
cat > /etc/dbus-1/system.d/bluealsa.conf <<'DBUSEOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own="org.bluealsa"/>
    <allow send_destination="org.bluealsa"/>
  </policy>
  <policy user="phonon">
    <allow own="org.bluealsa"/>
    <allow send_destination="org.bluealsa"/>
  </policy>
  <policy context="default">
    <allow send_destination="org.bluealsa"/>
  </policy>
</busconfig>
DBUSEOF

# BT Agent script (auto-accept pairing + auto-trust)
cp "${REPO_ROOT}/deploy/bt-agent.py" /opt/phonon/bt-agent.py 2>/dev/null || true
chmod +x /opt/phonon/bt-agent.py 2>/dev/null || true

# BT Agent systemd service
cat > /etc/systemd/system/phonon-bt-agent.service <<'BTAGENTSVC'
[Unit]
Description=Phonon Bluetooth Agent (auto-accept pairing)
After=bluetooth.service
Requires=bluetooth.service
[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/phonon/bt-agent.py
Restart=on-failure
RestartSec=3
[Install]
WantedBy=bluetooth.target
BTAGENTSVC

# BT rfkill unblock service
cat > /etc/systemd/system/phonon-bt-unblock.service <<'BTUNBLOCK'
[Unit]
Description=Unblock Bluetooth for Phonon
Before=bluetooth.service
[Service]
Type=oneshot
ExecStart=/usr/sbin/rfkill unblock bluetooth
[Install]
WantedBy=multi-user.target
BTUNBLOCK

# Sudoers for phonon (rfkill + hciconfig + PTP service control without password)
cat > /etc/sudoers.d/phonon <<'SUDOERS'
phonon ALL=(ALL) NOPASSWD: /usr/sbin/rfkill
phonon ALL=(ALL) NOPASSWD: /usr/sbin/hciconfig
phonon ALL=(ALL) NOPASSWD: /usr/bin/rfkill
phonon ALL=(ALL) NOPASSWD: /bin/hciconfig
phonon ALL=(ALL) NOPASSWD: /bin/systemctl start phonon-ptp4l.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop phonon-ptp4l.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl enable phonon-ptp4l.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable phonon-ptp4l.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl start phonon-phc2sys.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop phonon-phc2sys.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl enable phonon-phc2sys.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable phonon-phc2sys.service
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-update
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-bt-reset
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-bt-list-bonds
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-ptp-query port
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-ptp-query current
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-ptp-query parent
# System-instance services controllable from the UI's System panel
# (start/stop/restart only — the daemon doesn't need to enable/disable).
phonon ALL=(ALL) NOPASSWD: /bin/systemctl is-active *
phonon ALL=(ALL) NOPASSWD: /bin/systemctl start bluetooth.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop bluetooth.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl restart bluetooth.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl enable bluetooth.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable bluetooth.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl start bluealsa.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop bluealsa.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl restart bluealsa.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl enable bluealsa.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable bluealsa.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl start avahi-daemon.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop avahi-daemon.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl restart avahi-daemon.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl enable avahi-daemon.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable avahi-daemon.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop avahi-daemon.socket
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable avahi-daemon.socket
phonon ALL=(ALL) NOPASSWD: /bin/systemctl start phonon-bt-agent.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop phonon-bt-agent.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl restart phonon-bt-agent.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl enable phonon-bt-agent.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable phonon-bt-agent.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl start phonon-bt-unblock.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl stop phonon-bt-unblock.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl restart phonon-bt-unblock.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl enable phonon-bt-unblock.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl disable phonon-bt-unblock.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl restart phonon-ptp4l.service
phonon ALL=(ALL) NOPASSWD: /bin/systemctl restart phonon-phc2sys.service
# NTP helper — writes chrony source drop-in + chronyc reload/sync.
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-ntp write-sources *
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-ntp sync
# Network helper — netplan apply with auto-revert window.
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-net apply-iface * *
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-net confirm
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-net cancel
phonon ALL=(ALL) NOPASSWD: /usr/local/sbin/phonon-net status
SUDOERS
chmod 440 /etc/sudoers.d/phonon

# Self-update plumbing: write the repo path so the daemon knows where the
# checkout lives, and symlink update.sh under a stable path so the sudoers
# grant above stays scoped to one file.
echo "${REPO_ROOT}" > "${CONFIG_DIR}/repo-path"
chmod 0644 "${CONFIG_DIR}/repo-path"

# The /system/update/status endpoint runs as the daemon (phonon user) and
# does `git fetch` against REPO_ROOT — that needs read+exec on every
# parent dir down to the repo, plus group write on .git/ so the fetch
# can update FETCH_HEAD and the objects store. When the repo lives in a
# user home (e.g. /home/manager/Phonon) the default 700 perms on the
# home directory block phonon from even traversing in. Fix both:
#   * +x on every ancestor so phonon can cd through
#   * group ownership = phonon, g+rwX recursive on the repo
# This is destructive enough that we only do it when a repo path is set
# AND it's not under /opt (where install.sh deployed copies live with
# perms already correct).
if [ -d "${REPO_ROOT}" ] && [ "${REPO_ROOT#/opt/}" = "${REPO_ROOT}" ]; then
    # Make every parent dir traversable by phonon (one-shot, no harm
    # if already done). 'o+x' alone — we don't add group/owner perms
    # to /home/<user> since phonon shouldn't read those.
    parent="${REPO_ROOT}"
    while [ "${parent}" != "/" ] && [ "${parent}" != "" ]; do
        chmod o+x "${parent}" 2>/dev/null || true
        parent="$(dirname "${parent}")"
    done

    # Idempotent group ownership + perms. Only run chgrp/chmod if the
    # repo's group or perms aren't already what we want — the previous
    # 'chgrp -R + chmod -R g+rwX every install' loop dirtied the working
    # tree (mode bits flipped to 755 on existing 644 files) so a
    # subsequent ff-pull refused. Detect and skip when state matches.
    needs_chmod=false
    if [ "$(stat -c '%G' "${REPO_ROOT}")" != "${PHONON_GROUP}" ]; then
        needs_chmod=true
    fi
    if ! find "${REPO_ROOT}" -maxdepth 2 -type d ! -perm -g+rx -print -quit | grep -q .; then
        :  # group rx already there everywhere — keep it
    else
        needs_chmod=true
    fi
    if [ "${needs_chmod}" = true ]; then
        chgrp -R "${PHONON_GROUP}" "${REPO_ROOT}" 2>/dev/null || true
        chmod -R g+rwX "${REPO_ROOT}" 2>/dev/null || true
        echo "  Repo perms granted to ${PHONON_GROUP} group"
    fi

    # Belt-and-braces: even when chmod doesn't run, tell git locally to
    # ignore exec-bit changes. install.sh historically chmod'd binaries
    # on each pass and git tracked the mode flips as 'modifications',
    # which then blocked the next ff-pull. core.fileMode=false makes
    # git stop tracking those flips.
    git -C "${REPO_ROOT}" config --local core.fileMode false 2>/dev/null || true

    # CVE-2022-24765 mitigation: git refuses to operate on a repo whose
    # owner differs from the caller. Whitelist this specific path.
    git config --system --add safe.directory "${REPO_ROOT}" 2>/dev/null || true
fi

if [ -f "${REPO_ROOT}/deploy/update.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/update.sh"
    ln -sf "${REPO_ROOT}/deploy/update.sh" /usr/local/sbin/phonon-update
    echo "  Self-update wired (sudo phonon-update available, log at /var/log/phonon/update.log)"
fi

if [ -f "${REPO_ROOT}/deploy/bt-reset.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/bt-reset.sh"
    ln -sf "${REPO_ROOT}/deploy/bt-reset.sh" /usr/local/sbin/phonon-bt-reset
    echo "  BT factory reset wired (sudo phonon-bt-reset available, log at /var/log/phonon/bt-reset.log)"
fi

if [ -f "${REPO_ROOT}/deploy/bt-list-bonds.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/bt-list-bonds.sh"
    ln -sf "${REPO_ROOT}/deploy/bt-list-bonds.sh" /usr/local/sbin/phonon-bt-list-bonds
    echo "  BT bond enumerator wired (sudo phonon-bt-list-bonds available)"
fi

if [ -f "${REPO_ROOT}/deploy/ptp-query.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/ptp-query.sh"
    ln -sf "${REPO_ROOT}/deploy/ptp-query.sh" /usr/local/sbin/phonon-ptp-query
    echo "  PTP query wrapper wired (sudo phonon-ptp-query port|current|parent available)"
fi

if [ -f "${REPO_ROOT}/deploy/phonon-ntp.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/phonon-ntp.sh"
    ln -sf "${REPO_ROOT}/deploy/phonon-ntp.sh" /usr/local/sbin/phonon-ntp
    echo "  NTP helper wired (sudo phonon-ntp write-sources|sync available)"
fi

if [ -f "${REPO_ROOT}/deploy/phonon-net.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/phonon-net.sh"
    ln -sf "${REPO_ROOT}/deploy/phonon-net.sh" /usr/local/sbin/phonon-net
    echo "  Network helper wired (sudo phonon-net apply-iface|confirm|cancel|status available)"
fi

# Disable rival PipeWire stacks for non-phonon users. Two pipewire+
# wireplumber instances on the same hardware fight over ALSA mixer state,
# manifests as 0% hardware volumes after a reboot or stuck profiles.
# Idempotent — the script masks user units in each user's home, then
# stops live ones. Only phonon's stack stays.
if [ -f "${REPO_ROOT}/deploy/disable-rival-audio.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/disable-rival-audio.sh"
    PHONON_USER="${PHONON_USER}" bash "${REPO_ROOT}/deploy/disable-rival-audio.sh" \
        --phonon-user "${PHONON_USER}" 2>&1 | sed 's/^/  /'
fi

# Enable persistent journal
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null

# Reload — always pick up unit file changes
systemctl daemon-reload

# Honor pre-existing user state captured at the top of step 8:
#   not-found  -> fresh install, enable + start (default behaviour)
#   enabled    -> keep enabled, restart so the new unit file takes effect
#   disabled   -> user explicitly disabled (DG60-only Pi, etc.) — do NOT
#                 re-enable, do NOT start; the unit file is updated but
#                 left inactive
#   masked     -> aggressive form of disabled; same handling as disabled
case "${BT_AGENT_PREVSTATE}" in
    enabled)
        systemctl restart phonon-bt-agent.service 2>/dev/null || true
        echo "  phonon-bt-agent restarted (kept enabled)"
        ;;
    disabled|masked)
        echo "  phonon-bt-agent left ${BT_AGENT_PREVSTATE} (user state preserved)"
        ;;
    *)
        # `|| true` is critical: under set -e + pipefail, a failed
        # restart silently aborts the whole install. On a fresh box
        # bluez/bluetoothd might not be ready yet (the .service depends
        # on bluetooth.target, which takes a moment to come up). We
        # don't want install.sh to die at step 8/11 and never reach
        # the phonon-stage user service setup at step 10.
        systemctl enable phonon-bt-agent.service --quiet 2>/dev/null || true
        systemctl restart phonon-bt-agent.service 2>/dev/null || true
        echo "  phonon-bt-agent enabled (first install)"
        ;;
esac

case "${BT_UNBLOCK_PREVSTATE}" in
    enabled)
        systemctl restart phonon-bt-unblock.service 2>/dev/null || true
        echo "  phonon-bt-unblock restarted (kept enabled)"
        ;;
    disabled|masked)
        echo "  phonon-bt-unblock left ${BT_UNBLOCK_PREVSTATE} (user state preserved)"
        ;;
    *)
        # `|| true` for the same reason as the agent block above.
        systemctl enable phonon-bt-unblock.service --quiet 2>/dev/null || true
        systemctl start phonon-bt-unblock.service 2>/dev/null || true
        echo "  phonon-bt-unblock enabled (first install)"
        ;;
esac

# -- Step 9/11: Install PTP (linuxptp) ----------------------------------------

echo "[9/11] Installing PTP (linuxptp)..."

# Drop AES67 media-profile config files for ptp4l. We don't enable
# the services here — the Stage settings UI exposes a toggle so the
# user opts in once their network is wired and a grandmaster is
# present. Activation later: `systemctl enable --now phonon-ptp4l
# phonon-phc2sys`.

mkdir -p /etc/linuxptp

# AES67 media profile (per AES67 §6 + IEEE 1588 default profile,
# domain 0, multicast). The daemon (PtpSettings → renderer in
# phonon_stage.api.ptp) rewrites this file every time the user
# changes a value in the PTP tab — so this is just the
# fresh-install bootstrap. Skip if the file already exists, to
# preserve user customizations across re-installs.
if [ ! -f /etc/linuxptp/phonon-aes67.conf ]; then
    cat > /etc/linuxptp/phonon-aes67.conf <<'PTPCONF'
# Auto-generated by phonon-stage — edit Settings.ptp via UI/API.
# Manual edits will be overwritten on the next settings PATCH.

[global]
domainNumber              0
priority1                 128
priority2                 128
clockClass                248
clockAccuracy             0xfe
offsetScaledLogVariance   0xffff
slaveOnly                 0
serverOnly                0
free_running              0
freq_est_interval         1
dscp_event                46
dscp_general              46
network_transport         UDPv4
delay_mechanism           E2E
time_stamping             software
tx_timestamp_timeout      50
logAnnounceInterval       1
logSyncInterval           -3
logMinDelayReqInterval    -3
announceReceiptTimeout    3
hybrid_e2e                0
inhibit_multicast_service 0
clock_servo               pi
step_threshold            0.000002
first_step_threshold      0.000020
max_frequency             900000000
PTPCONF
fi
# Daemon needs to be able to rewrite this file when settings change.
# install.sh runs as root, so the cat-redirect above writes it as root.
# Hand it to phonon so the user-space daemon can overwrite atomically
# via tmp+rename without sudo.
chown "${PHONON_USER}:${PHONON_GROUP}" /etc/linuxptp/phonon-aes67.conf
chmod 0644 /etc/linuxptp/phonon-aes67.conf

# Default interface comes from /etc/default/phonon-ptp; auto-pick if absent.
cat > /etc/default/phonon-ptp <<'PTPDEFAULTS'
# Interface ptp4l/phc2sys bind to. Empty = auto-detect first non-lo
# interface with link UP. Set explicitly for production (eth0, eth1…).
PTP_IFACE=

# Slave mode flag for ptp4l: empty = full BMCA election; "-s" = slave only.
PTP_MODE_FLAG=
PTPDEFAULTS

# ptp4l service — wraps the binary with our config + auto-iface helper
cat > /etc/systemd/system/phonon-ptp4l.service <<'PTPSVC'
[Unit]
Description=Phonon PTP4L (IEEE 1588 / AES67 media profile)
Documentation=man:ptp4l(8)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=-/etc/default/phonon-ptp
ExecStartPre=/bin/sh -c 'if [ -z "$PTP_IFACE" ]; then echo PTP_IFACE=$(ip -o link show up | awk -F: "/state UP/ && \$2 !~ /lo/ {print \$2; exit}" | tr -d " ") > /run/phonon-ptp.env; else echo PTP_IFACE=$PTP_IFACE > /run/phonon-ptp.env; fi'
EnvironmentFile=-/run/phonon-ptp.env
ExecStart=/usr/sbin/ptp4l -f /etc/linuxptp/phonon-aes67.conf -i ${PTP_IFACE} ${PTP_MODE_FLAG} -m
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
PTPSVC

# phc2sys service — slaves the system clock to PTP (or vice versa for
# software timestamping fallback)
cat > /etc/systemd/system/phonon-phc2sys.service <<'PHC2SYSSVC'
[Unit]
Description=Phonon phc2sys (PHC ↔ system clock sync)
Documentation=man:phc2sys(8)
After=phonon-ptp4l.service
Requires=phonon-ptp4l.service

[Service]
EnvironmentFile=-/run/phonon-ptp.env
ExecStart=/usr/sbin/phc2sys -a -r -m
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
PHC2SYSSVC

systemctl daemon-reload

# Capture pre-existing enable state so re-installs don't override a user
# who explicitly disabled PTP via the UI / systemctl.
PTP4L_PREVSTATE=$(systemctl is-enabled phonon-ptp4l.service 2>/dev/null || echo "not-found")
PHC2SYS_PREVSTATE=$(systemctl is-enabled phonon-phc2sys.service 2>/dev/null || echo "not-found")

# On x86_64 (Optiplex, Geekom, generic PCs), auto-enable PTP services at
# install time. These boxes are usually the Stage Core: they get a proper
# NIC with hardware timestamping (or at least software fallback), AES67
# is the design target, PTP needs to be up for the whole chain to make
# sense. Skipping the manual "go to the UI and toggle" step removes one
# friction point on fresh installs.
#
# On Pi (arm64) keep the opt-in default: the USB-Ethernet adapters have
# no PHC, software PTP is noisy, and most Pi-only standalone use cases
# don't need PTP at all. Enable manually via Settings or `systemctl`.
if [ "${PLATFORM}" = "x86_64" ]; then
    case "${PTP4L_PREVSTATE}" in
        enabled)
            echo "  phonon-ptp4l kept enabled (user state preserved)"
            ;;
        disabled|masked)
            echo "  phonon-ptp4l left ${PTP4L_PREVSTATE} (user state preserved)"
            ;;
        *)
            systemctl enable --now phonon-ptp4l.service 2>/dev/null || true
            echo "  phonon-ptp4l enabled + started (x86 default)"
            ;;
    esac
    case "${PHC2SYS_PREVSTATE}" in
        enabled)
            echo "  phonon-phc2sys kept enabled (user state preserved)"
            ;;
        disabled|masked)
            echo "  phonon-phc2sys left ${PHC2SYS_PREVSTATE} (user state preserved)"
            ;;
        *)
            systemctl enable --now phonon-phc2sys.service 2>/dev/null || true
            echo "  phonon-phc2sys enabled + started (x86 default)"
            ;;
    esac
else
    echo "  PTP installed but NOT enabled (Pi default — toggle in Stage UI when ready)"
fi

# -- Step 10/11: Setup user service + linger ----------------------------------

echo "[10/11] Installing phonon-stage user service..."

PHONON_UID=$(id -u "${PHONON_USER}")

# Set home dir (needed for systemd user config)
usermod -d "${DATA_DIR}" "${PHONON_USER}" 2>/dev/null || true

# Enable linger (PipeWire starts at boot without login)
touch "/var/lib/systemd/linger/${PHONON_USER}" 2>/dev/null || true

# Create user service
mkdir -p "${DATA_DIR}/.config/systemd/user"
cat > "${DATA_DIR}/.config/systemd/user/phonon-stage.service" <<USVC
[Unit]
Description=Phonon Stage Agent
After=pipewire.service wireplumber.service
Wants=pipewire.service wireplumber.service

[Service]
Type=exec
ExecStart=${VENV_DIR}/bin/phonon-stage --config ${CONFIG_DIR}/stage.yaml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
USVC

# ── Plugin user-services ─────────────────────────────────────────────
# Each source plugin ships a user-instance unit so phonon-stage can
# enable/start/restart it via `systemctl --user` without any sudo
# escalation. The plugin's settings (rendered config file) lives in
# ${DATA_DIR}/plugins/<plugin-name>/ — phonon-stage creates the file
# at first enable, the unit references it by absolute path.

# AirPlay v1 — shairport-sync 3.x / 4.x
# `Wants=` (not `Requires=`) on pipewire-pulse: shairport-sync will
# keep trying to connect to the PA socket if it's not ready, that's
# fine and more robust than a hard requirement that would put the
# unit into 'failed' state during a pipewire restart.
#
# `-o pa` is mandatory: shairport-sync 4.x on Ubuntu defaults to the
# ALSA backend even when the conf says `output_backend = "pa"` (the
# conf value is parsed but doesn't actually switch the backend at
# runtime — empirically reproduced 2026-05-11). Without -o pa, the
# audio bypasses the airplay_in null-sink the plugin sets up and
# WirePlumber auto-routes it to the default sink, defeating the
# whole Phonon routing matrix.
mkdir -p "${DATA_DIR}/plugins/airplay-v1"
cat > "${DATA_DIR}/.config/systemd/user/shairport-sync.service" <<APV1SVC
[Unit]
Description=AirPlay 1 receiver (Phonon plugin)
After=pipewire-pulse.service
Wants=pipewire-pulse.service

[Service]
Type=simple
ExecStart=/usr/bin/shairport-sync -c ${DATA_DIR}/plugins/airplay-v1/shairport-sync.conf -o pa
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
APV1SVC

# Spotify Connect — spotifyd. Same After/Wants pattern as
# shairport-sync (pipewire-pulse provides the PA socket spotifyd
# writes into). spotifyd reads its TOML config via --config-path,
# rendered by SpotifyV1Plugin on first enable. --no-daemon keeps it
# in foreground so systemd manages the lifecycle.
# `ConditionPathExists=` on the binary keeps the unit clean when
# spotifyd wasn't installable — the plugin's runtime check surfaces
# the inert state as `last_error` to the UI.
mkdir -p "${DATA_DIR}/plugins/spotify-v1"
cat > "${DATA_DIR}/.config/systemd/user/spotifyd.service" <<SPDSVC
[Unit]
Description=Spotify Connect receiver via spotifyd (Phonon plugin)
After=pipewire-pulse.service
Wants=pipewire-pulse.service
ConditionPathExists=/usr/bin/spotifyd

[Service]
Type=simple
ExecStart=/usr/bin/spotifyd --no-daemon --config-path ${DATA_DIR}/plugins/spotify-v1/spotifyd.conf
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
SPDSVC

# Remove the legacy librespot.service from earlier dev iterations —
# the plugin no longer references it; leaving it on disk would just
# clutter `systemctl --user list-unit-files`.
rm -f "${DATA_DIR}/.config/systemd/user/librespot.service"

# WirePlumber config (disable bluez5, use bluealsa instead)
mkdir -p "${DATA_DIR}/.config/wireplumber/wireplumber.conf.d"
cat > "${DATA_DIR}/.config/wireplumber/wireplumber.conf.d/90-phonon-bluetooth.conf" <<'WPCONF'
wireplumber.profiles = {
  main = {
    monitor.bluez = disabled
    monitor.bluez-midi = disabled
    monitor.bluez.seat-monitoring = disabled
  }
}
WPCONF

chown -R "${PHONON_USER}:${PHONON_GROUP}" "${DATA_DIR}/.config"
# Plugin data dirs (created above with mkdir -p as root) need to be
# writable by the daemon — it renders settings into env/conf files
# via write_text_atomic which uses a temp file in the same dir.
# Without this, the first enable of any plugin hits PermissionError.
chown -R "${PHONON_USER}:${PHONON_GROUP}" "${DATA_DIR}/plugins"
echo "  User service installed"

# -- Step 11/11: Start everything ---------------------------------------------

echo "[11/11] Starting services..."

# Ensure runtime dir
mkdir -p "/run/user/${PHONON_UID}"
chown "${PHONON_USER}:${PHONON_GROUP}" "/run/user/${PHONON_UID}"
chmod 700 "/run/user/${PHONON_UID}"

# Start user instance
systemctl start "user@${PHONON_UID}.service"
sleep 3

# Enable and (re)start phonon-stage. We use `restart` rather than
# `enable --now` because the latter is a no-op when the service is
# already running — re-running install.sh on a live host then would
# leave the OLD daemon process in place and the user would never see
# the new code take effect.
# Restart the user-instance systemd manager so group membership
# changes (e.g. systemd-journal added above) propagate to the
# eventual phonon-stage process. Just restarting phonon-stage is
# not enough — it inherits the manager's group set, which was
# captured when user@<uid>.service first started.
systemctl restart "user@${PHONON_UID}.service" 2>&1 || true
sleep 2

systemd-run --uid="${PHONON_USER}" --gid="${PHONON_GROUP}" \
    -p PAMName=login --pipe --wait -- \
    systemctl --user daemon-reload 2>&1 || true
systemd-run --uid="${PHONON_USER}" --gid="${PHONON_GROUP}" \
    -p PAMName=login --pipe --wait -- \
    systemctl --user enable phonon-stage 2>&1 || true
systemd-run --uid="${PHONON_USER}" --gid="${PHONON_GROUP}" \
    -p PAMName=login --pipe --wait -- \
    systemctl --user restart phonon-stage 2>&1 || true

sleep 3

# Detect bind IP for display
BIND_IP=$(grep bind_address "${CONFIG_DIR}/stage.yaml" 2>/dev/null | awk '{print $2}' | tr -d '"' || echo "localhost")

# If we installed the lowlatency kernel this run, the running system is
# still on the generic kernel until reboot. Phonon will run, just won't
# get the audio scheduling benefit until then.
if [ "${NEEDS_REBOOT_FOR_LOWLATENCY}" = true ]; then
    echo ""
    echo "[REBOOT NEEDED] linux-lowlatency was installed but isn't active yet."
    echo "  Current kernel: $(uname -r) — reboot to switch to the lowlatency one."
    echo "  After reboot, verify with: uname -r   (should end in '-lowlatency')"
fi

# Friendly tip if the human operator (the user who ran sudo bash install.sh)
# doesn't have NOPASSWD configured. install.sh works fine without it — phonon
# daemon gets its own targeted NOPASSWD grants regardless. But re-running
# install.sh, deploying updates, running 'phonon-update', and any remote
# admin via SSH all benefit from skipping the password prompt.
SSH_USER="${SUDO_USER:-}"
if [ -n "${SSH_USER}" ] && [ "${SSH_USER}" != "root" ]; then
    # Re-probe NOPASSWD as the user, not as root.
    if ! sudo -u "${SSH_USER}" -n true 2>/dev/null; then
        echo ""
        echo "[TIP] User '${SSH_USER}' has to type its password for sudo."
        echo "      For remote admin / re-installs to run uninterrupted, add:"
        echo ""
        echo "      sudo bash -c 'echo \"${SSH_USER} ALL=(ALL) NOPASSWD: ALL\" > /etc/sudoers.d/90-${SSH_USER}-nopasswd && chmod 0440 /etc/sudoers.d/90-${SSH_USER}-nopasswd'"
        echo ""
        echo "      (See README.md 'First-time setup' for full rationale.)"
    fi
fi

if curl -s -o /dev/null -w "%{http_code}" "http://${BIND_IP}:8401/health" 2>/dev/null | grep -q "200"; then
    echo ""
    echo "========================================="
    echo "  Phonon Stage Agent installed and running"
    echo "  Platform: ${PLATFORM} (${DISTRO} ${DISTRO_VERSION})"
    echo "  Config:   ${CONFIG_DIR}/stage.yaml"
    echo "  UI:       http://${BIND_IP}:8401/standalone/"
    echo "========================================="
else
    echo ""
    echo "[WARNING] UI not responding yet. Services may still be starting."
    echo "  Check: systemd-run --uid=phonon -p PAMName=login --pipe --wait -- systemctl --user status phonon-stage"
fi
