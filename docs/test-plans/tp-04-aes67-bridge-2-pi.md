# TP-04 — AES67 bridge between two Pi Stages (canonical scenario)

**Goal**: validate the two-Stage standalone use case from CLAUDE.md §9 — two
Pi 3B running phonon-stage in `STANDALONE` mode, bridging audio in both
directions over AES67 multicast on a wired switch, no Controller involved.

**Hardware**:
- 2× Raspberry Pi 3B with Phonon installed (TP-01 passing on each)
- 1× small ethernet switch (UniFi or anything with proper IGMP)
- 2× short ethernet cables
- 1× phone with Bluetooth (audio source for Pi #1)
- 1× JBL Bluetooth speaker (audio sink for Pi #2)
- 1× UD100 Sena dongle on Pi #2 (drives the JBL)
- 1× DG60 Avantree dongle on Pi #1 (receives from the phone)

**Estimated time**: 30 minutes (15 setup, 10 audio test, 5 teardown).

## Topology

```
   ┌──────── Pi #1 (stage-x01) ────────┐    ┌──────── Pi #2 (stage-x02) ────────┐
   │                                   │    │                                   │
   │  [phone]──A2DP──>(DG60)─>bt_in────┼──> aes67-send-to-x02 ─multicast──>     │
   │                                   │    │  aes67-recv-from-x01 ─>(UD100)──> │
   │                                   │    │                                   ├─> JBL
   │                                   │    │                                   │
   └───────────────────────────────────┘    └───────────────────────────────────┘
                                                          │
                                                  ─────[switch]─────
```

## Pre-requisites

- [ ] TP-01 passed on both Pi
- [ ] Both Pi connected to the same switch via ethernet (not WiFi —
      AES67 over wireless is unreliable, see CLAUDE.md §3)
- [ ] `linuxptp` installed via the install.sh (TP-01 step 1)
- [ ] DG60 paired with the phone (long-press on DG60, accept on phone)
- [ ] UD100 paired with the JBL (via Pi #2 UI Bluetooth panel)

## Setup

### 1. Network sanity

On both Pi:

```bash
ip -4 addr show | grep eth0
ping -c 3 <other-pi-ip>
```

- [ ] Both Pi reach each other on the lab subnet (no firewall block)
- [ ] Multicast routing works:
      ```bash
      sudo tcpdump -i eth0 -n 'host 239.69.10.0/24' &  # on Pi #2
      python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'hi', ('239.69.10.10', 5004))"  # on Pi #1
      ```
      tcpdump on Pi #2 must see the packet from Pi #1.

### 2. Pi #1: BT input bridge

In `http://<pi1>:8401/standalone/` → EDIT mode:

- [ ] Devices tab → Bluetooth → "Sync Inputs/Outputs" creates `bt_<phone>_in`
- [ ] General tab → Patch Bay → `<phone>-BT-In` appears in Sources

Play music on the phone:

- [ ] Mixer would show audio activity if a mapping existed (deferred — no
      mapping yet, this is just to confirm the bridge works)

### 3. Pi #1: AES67 send

Network tab → AES67:

- [ ] Create Send: name `to-x02`, group `239.69.10.10`, port 5004, loop OFF
      (we're truly inter-Pi, no localhost loopback needed)

After ~3s PW restart:

- [ ] `aes67-send-to-x02` appears in the Patch Bay Sinks
- [ ] Topbar badge flips to MESH

### 4. Pi #1: route BT → AES67 send

Patch Bay:

- [ ] Click `<phone>-BT-In` source
- [ ] Click `aes67-send-to-x02` sink → mapping creates
- [ ] Bezier line cyan between them

### 5. Pi #2: AES67 receive

Network tab → AES67:

- [ ] **Discovered via SAP** lists `aes67-send-to-x02` from Pi #1's IP
- [ ] Click SUBSCRIBE → recv stream auto-created with the right group/port

### 6. Pi #2: route AES67 → BT output

Patch Bay:

- [ ] `aes67-recv-to-x02` (or the auto-named recv) appears in Sources
- [ ] BT JBL appears in Sinks (after pairing + Sync Inputs/Outputs)
- [ ] Click source then sink → mapping creates

### 7. Audio path check

- [ ] Music playing on the phone is audible on the JBL within 1-2 seconds
      of starting playback (allowing for codec + PTP stabilisation)
- [ ] No glitches/crackles after 60 seconds of continuous playback

### 8. PTP sanity (optional but recommended)

On both Pi: Settings → PTP → Enable, mode `auto`, interface `eth0`.

After ~10s:

- [ ] One Pi shows GM (magenta), the other shows SLAVE (green pulsing)
- [ ] Slave's offset is < 1 µs after 30s
- [ ] Topbar PTP badge stays green/magenta — no flapping

### 9. Reverse direction

Repeat steps 3-7 with roles swapped (mic on Pi #2 → speakers on Pi #1)
to validate full bidirectional bridge.

## Pass criteria

- All steps 1-7 ticked.
- Audio plays continuously for at least 5 minutes without dropouts.
- Topbar XRUN counter stays at 0 (or single-digit).
- PTP step (optional) showed sub-µs sync.

## Failure investigation

| Symptom | Where to look |
|---------|---------------|
| Pi #2's "Discovered via SAP" stays empty | `tcpdump -i eth0 -n port 9875` on Pi #2 — is the SAP packet arriving? |
| Audio glitches every few seconds | XRUN topbar badge. If non-zero: increase `ptime_ms` in Settings (1ms → 4ms) |
| Audio glitches at random | PTP sync — without it, clocks drift. Enable step 8. |
| No audio at all but mappings green | `pw-link -l` on each Pi — confirm links to bt_in / aes67 / alsa_output exist |
| Mappings keep going orphan | PW restart loop — check `journalctl --user-unit phonon-stage` |

## Teardown

- [ ] Stop the music
- [ ] DELETE both AES67 streams on each Pi (cleans the conf snippets)
- [ ] Disable PTP toggle if you enabled it
- [ ] Mappings auto-delete with the streams (or DELETE in Mixer)
