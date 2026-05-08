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
apt-get install -y -qq \
    python3-venv \
    python3-dev \
    libdbus-1-dev \
    pkg-config \
    avahi-daemon \
    pipewire \
    pipewire-alsa \
    pipewire-pulse \
    wireplumber \
    linuxptp \
    2>/dev/null

echo "  System packages OK"

# ── Step 3/7: Create phonon user ──────────���─────────────────────────────

echo "[3/11] Creating phonon user..."

if id -u "${PHONON_USER}" &>/dev/null; then
    echo "  User ${PHONON_USER} already exists"
else
    useradd -r -s /usr/sbin/nologin -d /nonexistent "${PHONON_USER}"
    echo "  User ${PHONON_USER} created"
fi

# Add to audio + bluetooth groups
usermod -aG audio "${PHONON_USER}" 2>/dev/null || true
usermod -aG bluetooth "${PHONON_USER}" 2>/dev/null || true
echo "  User in audio + bluetooth groups"

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

# Install python3-dbus + python3-gi for bt-agent
apt-get install -y -qq python3-dbus python3-gi bluez-alsa-utils 2>/dev/null || true

# Disable bluealsa-aplay (Phonon manages routing itself)
systemctl disable bluealsa-aplay 2>/dev/null || true
systemctl stop bluealsa-aplay 2>/dev/null || true

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
SUDOERS
chmod 440 /etc/sudoers.d/phonon

# Self-update plumbing: write the repo path so the daemon knows where the
# checkout lives, and symlink update.sh under a stable path so the sudoers
# grant above stays scoped to one file.
echo "${REPO_ROOT}" > "${CONFIG_DIR}/repo-path"
chmod 0644 "${CONFIG_DIR}/repo-path"

if [ -f "${REPO_ROOT}/deploy/update.sh" ]; then
    chmod +x "${REPO_ROOT}/deploy/update.sh"
    ln -sf "${REPO_ROOT}/deploy/update.sh" /usr/local/sbin/phonon-update
    echo "  Self-update wired (sudo phonon-update available, log at /var/log/phonon/update.log)"
fi

# Enable persistent journal
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null

# Reload and enable
systemctl daemon-reload
systemctl enable phonon-bt-agent.service --quiet 2>/dev/null
systemctl enable phonon-bt-unblock.service --quiet 2>/dev/null
systemctl restart phonon-bt-agent.service 2>/dev/null
systemctl start phonon-bt-unblock.service 2>/dev/null
echo "  BT services installed"

# -- Step 9/11: Install PTP (linuxptp) ----------------------------------------

echo "[9/11] Installing PTP (linuxptp)..."

# Drop AES67 media-profile config files for ptp4l. We don't enable
# the services here — the Stage settings UI exposes a toggle so the
# user opts in once their network is wired and a grandmaster is
# present. Activation later: `systemctl enable --now phonon-ptp4l
# phonon-phc2sys`.

mkdir -p /etc/linuxptp

# AES67 media profile (per AES67 §6 + IEEE 1588 default profile,
# domain 0, multicast). Override the interface in the systemd unit
# below (or via /etc/default/phonon-ptp).
cat > /etc/linuxptp/phonon-aes67.conf <<'PTPCONF'
[global]
domainNumber              0
priority1                 128
priority2                 128
clockClass                248
clockAccuracy             0xfe
offsetScaledLogVariance   0xffff
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
PTPCONF

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
EnvironmentFile=/etc/default/phonon-ptp
ExecStartPre=/bin/sh -c 'if [ -z "$PTP_IFACE" ]; then echo PTP_IFACE=$(ip -o link show up | awk -F: "/state UP/ && \$2 !~ /lo/ {print \$2; exit}" | tr -d " ") > /run/phonon-ptp.env; else echo PTP_IFACE=$PTP_IFACE > /run/phonon-ptp.env; fi'
EnvironmentFile=/run/phonon-ptp.env
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
echo "  PTP installed (services NOT enabled — use Stage UI to opt in)"

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

# Enable and start phonon-stage
systemd-run --uid="${PHONON_USER}" --gid="${PHONON_GROUP}" \
    -p PAMName=login --pipe --wait -- \
    systemctl --user daemon-reload 2>&1 || true
systemd-run --uid="${PHONON_USER}" --gid="${PHONON_GROUP}" \
    -p PAMName=login --pipe --wait -- \
    systemctl --user enable --now phonon-stage 2>&1 || true

sleep 3

# Detect bind IP for display
BIND_IP=$(grep bind_address "${CONFIG_DIR}/stage.yaml" 2>/dev/null | awk '{print $2}' | tr -d '"' || echo "localhost")

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
