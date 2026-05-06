# TP-01 — Stage bootstrap (fresh install)

**Goal**: validate that a clean Stage installation comes up healthy and
serves the standalone UI.

**Hardware**: any single Phonon Stage target (Pi 3B `stage-x01`, OptiPlex
3070, …). No peripherals required for this plan.

**Estimated time**: 10 minutes.

## Pre-requisites

- [ ] Fresh OS install (Raspberry Pi OS Bookworm 64-bit on Pi, Ubuntu Studio
  on x86) with network access.
- [ ] SSH access as a sudoer.
- [ ] Repo cloned at `~/phonon` (or anywhere — used by `install.sh`).

## Steps

### 1. Run the installer

```bash
cd ~/phonon
sudo bash deploy/install.sh
```

- [ ] Steps 1/11 through 11/11 all log without errors
- [ ] Final message confirms `phonon-stage.service` active

### 2. Verify systemd state

```bash
systemctl --user --machine=phonon@.host status phonon-stage.service
```

- [ ] `Active: active (running)`
- [ ] No `Failed` units in the unit listing

### 3. Verify HTTP endpoints

```bash
PORT=8401
IP=$(ip -4 addr show | awk '/inet / && !/127.0.0.1/ {print $2; exit}' | cut -d/ -f1)
curl -s http://${IP}:${PORT}/health | jq .
curl -s http://${IP}:${PORT}/capabilities | jq .
```

- [ ] `/health` returns `{"status":"ok", "uptime_seconds":..., "stage_id":"stage-..."}`
- [ ] `/capabilities` returns a non-empty audio_devices list (or empty if no card)
- [ ] `mode` is `STANDALONE`

### 4. Verify mDNS announce

From another machine on the same LAN:

```bash
avahi-browse -tr _phonon-stage._tcp
```

- [ ] The Stage's `stage-<hash>` appears with the correct IP and port 8401

### 5. Verify standalone UI

Open `http://<stage-ip>:8401/standalone/` in a browser.

- [ ] Topbar shows the stage_id, MESH/STANDALONE badge, clock
- [ ] LIVE / EDIT toggle works (LIVE locks edit-only sections)
- [ ] Tabs: General / Devices / Network / Settings — all render
- [ ] Mixer shows "No active mappings" (clean state)
- [ ] PTP badge shows `PTP—` or `PTP off` (linuxptp not configured)
- [ ] XRUN badge shows `0`

### 6. Verify install.sh idempotence

```bash
sudo bash deploy/install.sh    # second run
```

- [ ] No errors
- [ ] Final state identical (service still running, configs not duplicated)

### 7. Verify journalctl access

```bash
journalctl --user-unit phonon-stage.service -n 30 --no-pager
```

- [ ] Logs are JSON-structured, no Python tracebacks

## Pass criteria

All boxes ticked. Stage is ready for TP-02 (Controller discovery) or
TP-04 (2-Stage AES67 bridge).

## Common failure modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| 503 / "service not running" | systemd-linger missing | `sudo loginctl enable-linger phonon` |
| `/health` says ok but `mDNS` invisible | bind_address is 127.0.0.1 | Edit `/etc/phonon/stage.yaml`, restart |
| `capabilities.audio_devices` empty on Pi | HDMI audio not enabled | `hdmi_force_hotplug=1` in `/boot/firmware/config.txt`, reboot |
| WirePlumber claiming BT controllers | bluez monitor not disabled | Verify `~phonon/.config/wireplumber/wireplumber.conf.d/90-phonon-bluetooth.conf` exists |
