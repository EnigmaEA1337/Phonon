"""Network endpoints — NTP/clock status, interface info, QoS marking.

Phase 1:
  * slice 1: read-only NTP status panel
  * slice 2: edit NTP server list + manual sync
  * slice 3a: read-only interface table
  * slice 3b: edit interface IP / DHCP / static
  * slice 5: VLAN tagged iface + IP aliases ← this commit
  * slice 4: WiFi scan + connect
  * slice 6: PTP interface binding UI
  * slice 7: DSCP marking for AES67/PTP via nftables

Write paths use sudoers-granted scripts (`/usr/local/sbin/phonon-ntp`,
`phonon-net`) so we don't run the API server as root. The netplan
write path uses a systemd-run rollback timer for an auto-revert
window that makes the mgmt iface effectively un-brickable.

State management: every phonon-managed iface override + every VLAN
lives in /var/lib/phonon/network-state.json. Every apply rewrites
/etc/netplan/99-phonon-managed.yaml from this state in full — that
way adding a VLAN doesn't clobber a sibling iface override, and
the state file is the single source of truth for the UI.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

router = APIRouter(prefix="/network", tags=["network"])


class NtpSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str           # IP or hostname as chrony shows it
    state: str             # '*' = current sync source, '+' = combined, '-' = excluded, '?' = unreachable
    stratum: int
    reach: int             # 8-bit reachability bitmask
    last_rx_s: int         # seconds since last response
    offset_us: float       # last measured offset (microseconds)
    jitter_us: float       # +/- bound on the offset estimate (microseconds)


class NtpStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    daemon: str            # 'chrony' | 'timesyncd' | 'none'
    daemon_active: bool
    synchronized: bool     # `timedatectl` says system clock is in sync
    reference_id: str = ""
    reference_name: str = ""
    stratum: int = 0
    system_offset_us: float = 0.0      # how far our system clock is from NTP truth
    last_offset_us: float = 0.0        # most recent measurement
    rms_offset_us: float = 0.0         # rolling RMS
    frequency_ppm: float = 0.0         # local oscillator drift correction
    sources: list[NtpSource] = []
    notes: list[str] = []              # human-readable warnings (e.g. "chrony down, falling back to timesyncd")


async def _run(cmd: list[str], timeout: float = 3.0) -> tuple[int, str, str]:
    """Run a system command and capture stdout/stderr. Defensive — any
    failure returns (rc=-1, "", err) so callers can degrade gracefully
    instead of 500-ing the whole status endpoint."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode if proc.returncode is not None else -1
        return rc, out_b.decode("utf-8", errors="replace"), err_b.decode("utf-8", errors="replace")
    except Exception as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def _parse_chronyc_tracking(text: str) -> dict[str, object]:
    """Parse `chronyc -n tracking` output.

    Sample:
      Reference ID    : B97DBE7A (185.125.190.122)
      Stratum         : 3
      Ref time (UTC)  : Tue May 12 14:37:32 2026
      System time     : 0.000148048 seconds slow of NTP time
      Last offset     : -0.000165446 seconds
      RMS offset      : 0.000099690 seconds
      Frequency       : 3.443 ppm fast
    """
    out: dict[str, object] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "reference id":
            # "B97DBE7A (185.125.190.122)"  or  "00000000 ()"
            m = re.match(r"^(\S+)\s*(?:\(([^)]*)\))?\s*$", val)
            if m:
                out["reference_id"] = m.group(1)
                out["reference_name"] = m.group(2) or ""
        elif key == "stratum":
            try:
                out["stratum"] = int(val)
            except ValueError:
                pass
        elif key == "system time":
            # "0.000148048 seconds slow of NTP time" — positive = system clock is slow,
            # negative = system clock is fast. We store as signed microseconds.
            m = re.match(r"([0-9.]+)\s+seconds\s+(slow|fast)", val)
            if m:
                us = float(m.group(1)) * 1_000_000
                if m.group(2) == "fast":
                    us = -us
                out["system_offset_us"] = us
        elif key == "last offset":
            m = re.match(r"(-?[0-9.]+)\s+seconds", val)
            if m:
                out["last_offset_us"] = float(m.group(1)) * 1_000_000
        elif key == "rms offset":
            m = re.match(r"(-?[0-9.]+)\s+seconds", val)
            if m:
                out["rms_offset_us"] = float(m.group(1)) * 1_000_000
        elif key == "frequency":
            m = re.match(r"(-?[0-9.]+)\s+ppm\s+(fast|slow)", val)
            if m:
                ppm = float(m.group(1))
                if m.group(2) == "slow":
                    ppm = -ppm
                out["frequency_ppm"] = ppm
    return out


def _parse_chronyc_sources(text: str) -> list[NtpSource]:
    """Parse `chronyc -n sources`. Two header lines, then rows like:

      ^* 185.125.190.122               2  10   377   674   -544us[ -709us] +/-   14ms
       ↑   ↑                           ↑   ↑    ↑     ↑      ↑       ↑           ↑
       │   address                stratum poll reach age   offset  adj_offset   jitter
       state

    state char: '*' selected, '+' combined, '-' excluded, '?' unreachable,
    'x' falseticker, '~' time-too-variable.
    """
    sources: list[NtpSource] = []
    for raw in text.splitlines():
        # Body rows start with "^" or "=" then a state char + space + IP
        if not raw or len(raw) < 4:
            continue
        if not (raw[0] in "^=" and raw[1] in " *+-?x~"):
            continue
        state = raw[1]
        # Tokenise the rest on whitespace
        rest = raw[2:].strip()
        parts = rest.split()
        if len(parts) < 7:
            continue
        try:
            address = parts[0]
            stratum = int(parts[1])
            # parts[2] is poll interval, skip
            reach = int(parts[3], 8)  # octal as chronyc displays
            last_rx_s = _parse_lastrx(parts[4])
            # The offset column is rendered like "-544us[ -709us]" — chronyc
            # column-aligns the bracket which inserts variable internal
            # whitespace, so naive index lookups break. Anchor on the "+/-"
            # token instead: offset is everything before, jitter is the
            # one token after.
            try:
                pivot = parts.index("+/-")
            except ValueError:
                continue
            # Offset = the token at parts[5] with any `[...]` suffix stripped.
            offset_us = _parse_us_or_ms(re.split(r"[\[\]]", parts[5])[0])
            jitter_us = _parse_us_or_ms(parts[pivot + 1]) if pivot + 1 < len(parts) else 0.0
        except (ValueError, IndexError):
            continue
        sources.append(
            NtpSource(
                address=address,
                state=state,
                stratum=stratum,
                reach=reach,
                last_rx_s=last_rx_s,
                offset_us=offset_us,
                jitter_us=jitter_us,
            )
        )
    return sources


def _parse_lastrx(s: str) -> int:
    """chronyc reports LastRx as a bare integer of seconds, or with a
    suffix when it's been minutes/hours (e.g. '12m', '3h')."""
    s = s.strip()
    if not s:
        return 0
    try:
        if s.endswith("m"):
            return int(s[:-1]) * 60
        if s.endswith("h"):
            return int(s[:-1]) * 3600
        if s.endswith("d"):
            return int(s[:-1]) * 86400
        return int(s)
    except ValueError:
        return 0


def _parse_us_or_ms(s: str) -> float:
    """Convert a chronyc value like '-544us' / '14ms' / '0.5s' to
    microseconds (float). Returns 0.0 if the unit is unrecognised."""
    s = s.strip().lstrip("+")
    m = re.match(r"^(-?[0-9.]+)\s*(us|ms|s|ns)$", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "us":
        return val
    if unit == "ns":
        return val / 1000.0
    if unit == "ms":
        return val * 1000.0
    if unit == "s":
        return val * 1_000_000.0
    return 0.0


async def _detect_daemon() -> tuple[str, bool]:
    """Return (name, active) for whichever NTP daemon owns the clock.
    Probes chrony first (preferred for sub-ms accuracy) then timesyncd."""
    # chrony service is sometimes 'chrony', sometimes 'chronyd' depending on distro
    for name in ("chrony", "chronyd"):
        rc, out, _ = await _run(["systemctl", "is-active", name])
        if rc == 0 and out.strip() == "active":
            return "chrony", True
    rc, out, _ = await _run(["systemctl", "is-active", "systemd-timesyncd"])
    if rc == 0 and out.strip() == "active":
        return "timesyncd", True
    return "none", False


async def _is_synchronized() -> bool:
    """`timedatectl show -p NTPSynchronized --value` returns 'yes'/'no'."""
    rc, out, _ = await _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return rc == 0 and out.strip() == "yes"


@router.get("/ntp", response_model=NtpStatus)
async def get_ntp_status() -> NtpStatus:
    """Read-only snapshot of the local clock source.

    Phase 1 only — no mutations. Used by the Network tab to surface
    NTP offset / sync state so the operator can correlate shairport
    timing drift, AES67 jitter, etc. with clock-domain health.
    """
    daemon, active = await _detect_daemon()
    sync = await _is_synchronized()
    notes: list[str] = []

    status = NtpStatus(
        daemon=daemon,
        daemon_active=active,
        synchronized=sync,
    )

    if daemon == "chrony" and active:
        rc, tracking_out, tracking_err = await _run(["chronyc", "-n", "tracking"])
        if rc == 0:
            parsed = _parse_chronyc_tracking(tracking_out)
            status.reference_id = str(parsed.get("reference_id", ""))
            status.reference_name = str(parsed.get("reference_name", ""))
            status.stratum = int(parsed.get("stratum", 0) or 0)
            status.system_offset_us = float(parsed.get("system_offset_us", 0.0) or 0.0)
            status.last_offset_us = float(parsed.get("last_offset_us", 0.0) or 0.0)
            status.rms_offset_us = float(parsed.get("rms_offset_us", 0.0) or 0.0)
            status.frequency_ppm = float(parsed.get("frequency_ppm", 0.0) or 0.0)
        else:
            notes.append(f"chronyc tracking failed: rc={rc} {tracking_err.strip()[:120]}")

        rc, sources_out, sources_err = await _run(["chronyc", "-n", "sources"])
        if rc == 0:
            status.sources = _parse_chronyc_sources(sources_out)
        else:
            notes.append(f"chronyc sources failed: rc={rc} {sources_err.strip()[:120]}")

    elif daemon == "timesyncd" and active:
        # timesyncd doesn't expose per-source offsets like chrony, but
        # `timedatectl show-timesync` gives the server + the last sync.
        rc, out, _ = await _run(["timedatectl", "show-timesync", "--all"])
        if rc == 0:
            srv = ""
            for line in out.splitlines():
                if line.startswith("ServerName="):
                    srv = line.split("=", 1)[1].strip()
            if srv:
                status.reference_name = srv
            notes.append("Using systemd-timesyncd — offset / jitter not available. "
                        "Install chrony for sub-ms PTP-grade clock metrics.")
    else:
        notes.append("No NTP daemon active on this Stage. Install chrony.")

    status.notes = notes
    return status


# ─────────────────────────────────────────────────────────
# Slice 2 — NTP write path
# ─────────────────────────────────────────────────────────

# Where phonon writes its managed chrony server list. Standard
# distro config (/etc/chrony/chrony.conf) typically `sourcedir`s
# /etc/chrony/sources.d so a drop-in is preferred over editing the
# main file — keeps OS upgrades clean.
_CHRONY_SOURCES_DROPIN = "/etc/chrony/sources.d/phonon-servers.conf"

# Single shared helper for invocations of the privileged helper script.
# install.sh sets a NOPASSWD sudoers entry on /usr/local/sbin/phonon-ntp.
_PHONON_NTP_BIN = "/usr/local/sbin/phonon-ntp"


class NtpConfig(BaseModel):
    """Phonon-managed chrony config. We only own the server list and
    the optional `pool` shorthand — the rest of /etc/chrony/chrony.conf
    is the distro default and stays untouched."""

    model_config = ConfigDict(extra="forbid")
    # Each entry is "server <addr> [opts]" or "pool <addr> [opts]" — we
    # store the *full* line so the operator can paste a `pool 2.debian.pool.ntp.org iburst`
    # or `server time.cloudflare.com iburst` and we just take it verbatim.
    # Validated as non-empty and trimmed; the helper script does the
    # syntax check on chrony's side.
    servers: list[str]


class NtpApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    message: str


def _validate_server_line(line: str) -> str:
    """Trim + reject obvious garbage. We DON'T enforce a strict syntax
    — chrony itself rejects bad lines on reload, and forcing a regex
    here would break legitimate but exotic options (e.g. `iburst
    minpoll 4 maxpoll 10`). The helper script does `chronyd -Q` to
    smoke-test before installing."""
    s = line.strip()
    if not s:
        raise ValueError("empty server line")
    if "\n" in s or "\r" in s:
        raise ValueError("newline in server line")
    # Block shell metacharacters that could escape the conf format.
    # chrony's parser would barf anyway but we belt-and-brace it here
    # since the line lands in a sudoed conf write.
    bad = set("`$&|;><\"'\\")
    if any(c in s for c in bad):
        raise ValueError(f"forbidden character in server line: {s!r}")
    head = s.split(None, 1)[0].lower()
    if head not in {"server", "pool", "peer"}:
        raise ValueError(f"server line must start with 'server', 'pool', or 'peer': {s!r}")
    return s


@router.get("/ntp/config", response_model=NtpConfig)
async def get_ntp_config() -> NtpConfig:
    """Return the phonon-managed server list (parsed from the drop-in)."""
    try:
        from pathlib import Path
        path = Path(_CHRONY_SOURCES_DROPIN)
        if not path.exists():
            return NtpConfig(servers=[])
        lines = []
        for raw in path.read_text().splitlines():
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            lines.append(s)
        return NtpConfig(servers=lines)
    except Exception as exc:
        logger.warning("ntp.config_read_failed", exc_info=True)
        # Empty config rather than 500 — keeps the UI usable so the
        # operator can still push a fresh list.
        return NtpConfig(servers=[])


@router.put("/ntp/config", response_model=NtpApplyResult)
async def put_ntp_config(cfg: NtpConfig) -> NtpApplyResult:
    """Replace the phonon drop-in + reload chrony. Atomic on the helper
    side (write to a tempfile, fsync, rename, chronyc reload sources)."""
    validated: list[str] = []
    for raw in cfg.servers:
        try:
            validated.append(_validate_server_line(raw))
        except ValueError as exc:
            return NtpApplyResult(ok=False, message=str(exc))
    if len(validated) > 32:
        return NtpApplyResult(ok=False, message="too many entries (limit 32)")
    body = "# Managed by phonon-stage. Edits here are overwritten.\n"
    body += "\n".join(validated) + "\n"
    rc, out, err = await _run(["sudo", "-n", _PHONON_NTP_BIN, "write-sources", body], timeout=10.0)
    if rc != 0:
        return NtpApplyResult(ok=False, message=f"helper failed rc={rc}: {(err or out)[:200]}")
    return NtpApplyResult(ok=True, message=f"{len(validated)} servers applied, chrony reloaded")


@router.post("/ntp/sync", response_model=NtpApplyResult)
async def trigger_sync() -> NtpApplyResult:
    """`chronyc -a burst 4/4 + makestep` — forces a fast re-sync. Useful
    after a power cycle when the clock is way off, or after a server
    list edit. Requires authority over chrony (sudo via the helper)."""
    rc, out, err = await _run(["sudo", "-n", _PHONON_NTP_BIN, "sync"], timeout=10.0)
    if rc != 0:
        return NtpApplyResult(ok=False, message=f"helper failed rc={rc}: {(err or out)[:200]}")
    return NtpApplyResult(ok=True, message=out.strip()[:200] or "sync requested")


# ─────────────────────────────────────────────────────────
# Slice 3a — read-only interface table
# ─────────────────────────────────────────────────────────


class IfAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: str       # "inet" | "inet6"
    address: str
    prefix: int
    scope: str        # "global" | "link" | "host"
    dynamic: bool     # True iff DHCP-leased
    label: str = ""   # for IP aliases, e.g. "enp1s0:0"


class EthtoolCaps(BaseModel):
    """Subset of `ethtool -T <iface>` output that matters for PTP /
    AES67 — namely whether the NIC owns a hardware clock and the
    timestamping flags ptp4l checks. The remaining ethtool surface
    (filter modes, driver caps) lives in the helper's verbose path
    if we ever need it."""

    model_config = ConfigDict(extra="forbid")
    # Capability flags from the "Capabilities:" block. Names mirror
    # ethtool's keyword form ("hardware-transmit") so the UI can show
    # exactly what the operator would see at the shell.
    hw_transmit: bool = False
    hw_receive: bool = False
    hw_raw_clock: bool = False
    sw_transmit: bool = False
    sw_receive: bool = False
    sw_system_clock: bool = False
    # PHC index — -1 means no PTP hardware clock. ptp4l's hardware
    # timestamping path needs phc_index >= 0 AND all three hw_* flags.
    phc_index: int = -1
    # Computed: True iff this NIC can give ptp4l a real hardware
    # timestamp (sub-microsecond). Drives the "HW-PTP" badge.
    hw_ptp_capable: bool = False
    # Best-effort driver name (from `ethtool -i` second-best — we
    # already capture this via networkctl status, but ethtool -T
    # surfaces it too).
    raw_available: bool = True  # False iff ethtool -T failed for this iface


class IfaceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: str         # "ether" | "loopback" | "wifi" | "vlan" | "other"
    operstate: str    # "up" | "down" | "unknown"
    is_mgmt: bool     # carries the current API request — protected from edit
    mac: str = ""
    mtu: int = 0
    speed_mbps: int = 0          # 0 = unknown (down / loopback / virtual)
    duplex: str = ""             # "full" | "half" | ""
    addresses: list[IfAddress] = []
    gateway4: str = ""           # default route nexthop
    dns: list[str] = []
    vlan_parent: str = ""        # set iff this is a VLAN child
    vlan_id: int = 0
    # systemd-networkd / netplan-applied state
    networkd_setup: str = ""     # "configured" | "configuring" | "unmanaged"
    online: bool = False         # `networkctl` reports it as online
    # PTP / timestamping capability (None when ethtool isn't applicable
    # — loopback, virtual ifaces, or ethtool unavailable on this host).
    ethtool: EthtoolCaps | None = None


async def _ip_addr_json() -> list[dict]:
    rc, out, _ = await _run(["ip", "-j", "addr"], timeout=3.0)
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


async def _ip_route_json() -> list[dict]:
    rc, out, _ = await _run(["ip", "-j", "route"], timeout=3.0)
    if rc != 0:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def parse_ethtool_T(text: str) -> EthtoolCaps:
    """Parse `ethtool -T <iface>` output into our EthtoolCaps model.

    Sample input:
      Time stamping parameters for enp1s0:
      Capabilities:
              hardware-transmit
              software-transmit
              hardware-receive
              software-receive
              software-system-clock
              hardware-raw-clock
      PTP Hardware Clock: 0
      Hardware Transmit Timestamp Modes: ...
      Hardware Receive Filter Modes: ...

    Older ethtool builds and some drivers use the SOF_TIMESTAMPING_*
    constants instead of the keyword form — we accept either.
    """
    caps = EthtoolCaps()
    if not text:
        caps.raw_available = False
        return caps
    # Capability flags — both spelling families
    caps.hw_transmit = ("hardware-transmit" in text) or ("SOF_TIMESTAMPING_TX_HARDWARE" in text)
    caps.hw_receive = ("hardware-receive" in text) or ("SOF_TIMESTAMPING_RX_HARDWARE" in text)
    caps.hw_raw_clock = ("hardware-raw-clock" in text) or ("SOF_TIMESTAMPING_RAW_HARDWARE" in text)
    caps.sw_transmit = ("software-transmit" in text) or ("SOF_TIMESTAMPING_TX_SOFTWARE" in text)
    caps.sw_receive = ("software-receive" in text) or ("SOF_TIMESTAMPING_RX_SOFTWARE" in text)
    caps.sw_system_clock = ("software-system-clock" in text) or ("SOF_TIMESTAMPING_SOFTWARE" in text)
    # PHC line. "PTP Hardware Clock: <n>" with n a non-negative int, or
    # "none"/absent when the NIC has no PHC.
    m = re.search(r"PTP Hardware Clock:\s*(\S+)", text)
    if m:
        v = m.group(1)
        caps.phc_index = int(v) if v.isdigit() else -1
    # Computed: ptp4l hardware path needs the trifecta + a real PHC.
    caps.hw_ptp_capable = (
        caps.hw_transmit and caps.hw_receive and caps.hw_raw_clock and caps.phc_index >= 0
    )
    return caps


async def _get_ethtool_caps(iface: str) -> EthtoolCaps | None:
    """Run `ethtool -T <iface>` and parse. Returns None when ethtool
    isn't applicable (loopback, virtual ifaces) or unavailable on
    the host."""
    if not iface:
        return None
    rc, out, err = await _run(["ethtool", "-T", iface], timeout=2.5)
    if rc != 0:
        # ethtool says "No such device" on virtual / loopback / bridge
        # ifaces, "Operation not supported" on some USB-Eth chips.
        # Return a caps object marked raw_available=False so the UI
        # can show "ethtool: n/a" rather than dropping the iface entirely.
        caps = EthtoolCaps()
        caps.raw_available = False
        return caps
    return parse_ethtool_T(out)


async def _networkctl_status(iface: str) -> dict[str, str]:
    """Parse the human-readable `networkctl status <iface>` output for
    the fields `ip` doesn't expose: setup state, online flag, DNS,
    link speed/duplex. networkctl --json exists but is finicky across
    distros — text parse is more portable here."""
    rc, out, _ = await _run(["networkctl", "status", iface], timeout=3.0)
    if rc != 0:
        return {}
    fields: dict[str, str] = {}
    for raw in out.splitlines():
        m = re.match(r"^\s*([A-Za-z][A-Za-z0-9 ()/]*?)\s*:\s+(.*)$", raw)
        if not m:
            continue
        key = m.group(1).strip().lower()
        val = m.group(2).strip()
        # Preserve only the first occurrence of a key — networkctl
        # repeats some labels for multiple addresses, we want the line
        # closest to the field header.
        fields.setdefault(key, val)
    return fields


def _detect_mgmt_iface(request: Request) -> str:
    """Return the interface name carrying this API request, so the UI
    can flag it as protected. Falls back to "" if we can't tell —
    the UI then treats nothing as mgmt (everything editable). We never
    auto-block more than necessary; safer to ask the user to confirm
    when in doubt than to over-restrict editing."""
    # FastAPI exposes the local socket via request.scope. The local IP
    # is the one the client connected to — we then find which iface
    # owns that IP.
    try:
        local = request.scope.get("server")  # (host, port)
        local_ip = local[0] if local else ""
    except Exception:
        local_ip = ""
    if not local_ip or local_ip in ("0.0.0.0", "127.0.0.1", "::"):
        return ""
    # Match the IP against the kernel addr list. Synchronous fallback
    # because we're in a non-async helper called from inside the
    # endpoint — and `ip -o addr` is cheap.
    try:
        import subprocess
        out = subprocess.run(
            ["ip", "-j", "addr"], capture_output=True, text=True, timeout=2.0
        )
        if out.returncode != 0:
            return ""
        for nd in json.loads(out.stdout):
            for a in nd.get("addr_info", []):
                if a.get("local") == local_ip:
                    return nd.get("ifname", "")
    except Exception:
        pass
    return ""


def _classify_iface(nd: dict) -> str:
    """Map `ip -j addr`'s loose hints into our own type taxonomy."""
    link_type = nd.get("link_type", "")
    linkinfo = nd.get("linkinfo") or {}
    info_kind = linkinfo.get("info_kind", "")
    if info_kind == "vlan":
        return "vlan"
    if link_type == "loopback":
        return "loopback"
    name = nd.get("ifname", "")
    # iw / wireless heuristic — checked via sysfs in the read path
    # later if we want to be exact. For now name-prefix detection is
    # good enough for the table.
    if name.startswith(("wl", "wlan")) or Path(f"/sys/class/net/{name}/wireless").exists():
        return "wifi"
    if link_type == "ether":
        return "ether"
    return "other"


async def _build_iface_info(nd: dict, routes: list[dict], mgmt: str) -> IfaceInfo:
    name = nd.get("ifname", "")
    iface_type = _classify_iface(nd)
    addresses: list[IfAddress] = []
    for a in nd.get("addr_info", []):
        addresses.append(
            IfAddress(
                family=a.get("family", ""),
                address=a.get("local", ""),
                prefix=int(a.get("prefixlen", 0) or 0),
                scope=a.get("scope", ""),
                dynamic=bool(a.get("dynamic", False)),
                label=a.get("label", "") or "",
            )
        )
    # Default route for this iface (IPv4 only for now — IPv6 is
    # rarely interesting on a lab LAN and adds clutter).
    gateway4 = ""
    for r in routes:
        if r.get("dst") == "default" and r.get("dev") == name and r.get("gateway"):
            gateway4 = r["gateway"]
            break
    linkinfo = nd.get("linkinfo") or {}
    info_data = linkinfo.get("info_data") or {}
    vlan_parent = nd.get("link", "") if iface_type == "vlan" else ""
    vlan_id = int(info_data.get("id", 0) or 0) if iface_type == "vlan" else 0

    info = IfaceInfo(
        name=name,
        type=iface_type,
        operstate=str(nd.get("operstate", "")).lower(),
        is_mgmt=(name == mgmt),
        mac=nd.get("address", ""),
        mtu=int(nd.get("mtu", 0) or 0),
        addresses=addresses,
        gateway4=gateway4,
        vlan_parent=vlan_parent,
        vlan_id=vlan_id,
    )

    # ethtool -T capabilities — only meaningful for ether / vlan
    # ifaces; loopback and wifi rarely report useful timestamping data.
    if iface_type in ("ether", "vlan"):
        info.ethtool = await _get_ethtool_caps(name)

    # Enrich with networkctl-only fields (speed, duplex, dns, setup state).
    extra = await _networkctl_status(name)
    if "speed" in extra:
        m = re.match(r"(\d+)(?:Gbps|Mbps)", extra["speed"])
        if m:
            val = int(m.group(1))
            info.speed_mbps = val * 1000 if "Gbps" in extra["speed"] else val
    if "duplex" in extra:
        info.duplex = extra["duplex"]
    if "dns" in extra:
        info.dns = [s.strip() for s in extra["dns"].split() if s.strip()]
    if "online state" in extra:
        info.online = extra["online state"].lower() == "online"
    # networkctl reports SETUP via the list view (col 5); status doesn't
    # always include it. Best-effort match by parsing the link line.
    rc, list_out, _ = await _run(["networkctl", "list", "--no-pager", "--no-legend"], timeout=2.0)
    if rc == 0:
        for line in list_out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1] == name:
                info.networkd_setup = parts[4]
                break
    return info


# ─────────────────────────────────────────────────────────
# Slice 3b — interface IP/DHCP edit via netplan + auto-revert
# ─────────────────────────────────────────────────────────

_PHONON_NET_BIN = "/usr/local/sbin/phonon-net"
_PENDING_STATE_FILE = "/run/phonon-net/pending.json"


class IfaceConfig(BaseModel):
    """One iface's desired netplan state.

    `addresses4` is a list of CIDR strings — the FIRST element is the
    primary, the rest are IP aliases (which networkd just renders as
    additional addresses on the same interface). DHCP and static
    aren't mutually exclusive in netplan: you can keep dhcp4=true
    AND add manual addresses as aliases.
    """

    model_config = ConfigDict(extra="forbid")
    dhcp4: bool = True
    addresses4: list[str] = []   # CIDR list — [0] = primary, [1:] = aliases
    gateway4: str = ""           # IPv4 default gateway
    dns: list[str] = []          # ["1.1.1.1", "192.168.1.254"]
    mtu: int = 0                 # 0 = leave kernel default
    timeout_s: int = 120         # rollback window — clamped 30-600 by the helper


class VlanConfig(BaseModel):
    """A VLAN-tagged child interface. Lives in netplan's `vlans:`
    section alongside its IPv4 config — the child can be DHCP or
    static, just like a regular ethernet."""

    model_config = ConfigDict(extra="forbid")
    parent: str                  # parent iface (must exist as ether)
    vlan_id: int                 # 1-4094
    dhcp4: bool = True
    addresses4: list[str] = []
    gateway4: str = ""
    dns: list[str] = []
    mtu: int = 0
    timeout_s: int = 120


class NetworkState(BaseModel):
    """Phonon-managed network overrides. Whatever is here gets
    rewritten verbatim into /etc/netplan/99-phonon-managed.yaml on
    every apply."""

    model_config = ConfigDict(extra="forbid")
    ethernets: dict[str, IfaceConfig] = {}   # iface name → cfg
    vlans: dict[str, VlanConfig] = {}        # vlan child name → cfg


# Module-level singleton. Loaded by init() at app startup so the
# JSON file's owner stays phonon — main.py wires this from the
# lifespan handler so we don't conditionally import config at module
# scope.
_state: NetworkState = NetworkState()
_state_path: Path | None = None


def init(data_dir: Path) -> None:
    """Load persisted network state. Called once from main.py's
    lifespan startup so the path resolves to the same data root as
    plugins/mixer/settings."""
    global _state, _state_path
    _state_path = data_dir / "network-state.json"
    if _state_path.exists():
        try:
            _state = NetworkState.model_validate_json(_state_path.read_text())
            logger.info("network.state_loaded", path=str(_state_path))
        except Exception:
            logger.warning("network.state_load_failed", path=str(_state_path), exc_info=True)


def _save_state() -> None:
    if _state_path is None:
        return
    import contextlib
    _state_path.parent.mkdir(parents=True, exist_ok=True)
    _state_path.write_text(_state.model_dump_json(indent=2))
    with contextlib.suppress(OSError):
        _state_path.chmod(0o600)
    logger.info("network.state_saved", path=str(_state_path))


class ApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    message: str
    expires_at: int = 0       # epoch seconds; non-zero iff a rollback is pending


class PendingApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pending: bool
    expires_at: int = 0
    backup_dir: str = ""
    seconds_remaining: int = 0


_IPV4_CIDR_RE = re.compile(
    r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d{1,2})$"
)
_IPV4_RE = re.compile(
    r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$"
)


def _valid_ipv4(addr: str) -> bool:
    m = _IPV4_RE.match(addr)
    if not m:
        return False
    return all(0 <= int(g) <= 255 for g in m.groups())


def _valid_ipv4_cidr(addr: str) -> bool:
    m = _IPV4_CIDR_RE.match(addr)
    if not m:
        return False
    octets = [int(g) for g in m.groups()[:4]]
    prefix = int(m.group(5))
    return all(0 <= o <= 255 for o in octets) and 0 <= prefix <= 32


def _valid_iface_name(name: str) -> bool:
    """Linux iface names: 1-15 chars, alnum + dot/dash/underscore.
    Tighter than the kernel so we can paste it into YAML safely."""
    return bool(re.match(r"^[A-Za-z0-9._-]{1,15}$", name))


def _render_iface_body(lines: list[str], cfg: IfaceConfig | VlanConfig, indent: str) -> None:
    """Emit dhcp4 / addresses / routes / nameservers / mtu lines into
    `lines`. Shared between ethernet and VLAN renderers — VLANs have
    extra `id:` / `link:` lines on top of an otherwise identical
    body, so factoring the body keeps both render paths in sync."""
    lines.append(f"{indent}dhcp4: {'true' if cfg.dhcp4 else 'false'}")
    # `addresses` covers BOTH the primary static IP and any aliases.
    # When dhcp4 is on the addresses are additive (manual aliases on
    # top of the DHCP lease) — netplan handles that natively.
    if cfg.addresses4:
        lines.append(f"{indent}addresses: [{', '.join(cfg.addresses4)}]")
    if cfg.gateway4:
        lines.append(f"{indent}routes:")
        lines.append(f"{indent}  - to: default")
        lines.append(f"{indent}    via: {cfg.gateway4}")
    if cfg.dns:
        lines.append(f"{indent}nameservers:")
        lines.append(f"{indent}  addresses: [{', '.join(cfg.dns)}]")
    if cfg.mtu:
        lines.append(f"{indent}mtu: {cfg.mtu}")


def render_netplan_yaml(state: NetworkState) -> str:
    """Produce the full netplan YAML body from the in-memory state.
    Hand-rendered (no PyYAML dep) — the format is tiny and fully
    under our control."""
    lines = [
        "# Managed by phonon-stage. Edits here are overwritten.",
        "network:",
        "  version: 2",
        "  renderer: networkd",
    ]
    if state.ethernets:
        lines.append("  ethernets:")
        for name, cfg in state.ethernets.items():
            lines.append(f"    {name}:")
            _render_iface_body(lines, cfg, indent="      ")
    if state.vlans:
        lines.append("  vlans:")
        for name, cfg in state.vlans.items():
            lines.append(f"    {name}:")
            lines.append(f"      id: {cfg.vlan_id}")
            lines.append(f"      link: {cfg.parent}")
            _render_iface_body(lines, cfg, indent="      ")
    return "\n".join(lines) + "\n"


def _validate_addresses(addresses4: list[str]) -> str | None:
    if len(addresses4) > 8:
        return "too many addresses (max 8 per interface)"
    for a in addresses4:
        if not _valid_ipv4_cidr(a):
            return f"invalid IPv4 CIDR: {a!r}"
    return None


def _validate_iface_cfg(name: str, cfg: IfaceConfig) -> str | None:
    """Return None if cfg is valid, else an error string."""
    if not _valid_iface_name(name):
        return f"invalid iface name: {name!r}"
    if not cfg.dhcp4 and not cfg.addresses4:
        return "static config requires at least one address (CIDR form)"
    err = _validate_addresses(cfg.addresses4)
    if err:
        return err
    if cfg.gateway4 and not _valid_ipv4(cfg.gateway4):
        return f"invalid IPv4 gateway: {cfg.gateway4!r}"
    for d in cfg.dns:
        if not _valid_ipv4(d):
            return f"invalid DNS address: {d!r} (IPv4 only in this slice)"
    if cfg.mtu and not (576 <= cfg.mtu <= 9216):
        return f"MTU out of range (576-9216): {cfg.mtu}"
    if not (30 <= cfg.timeout_s <= 600):
        return f"timeout out of range (30-600s): {cfg.timeout_s}"
    return None


def _validate_vlan_cfg(name: str, cfg: VlanConfig) -> str | None:
    if not _valid_iface_name(name):
        return f"invalid VLAN name: {name!r}"
    if not _valid_iface_name(cfg.parent):
        return f"invalid parent iface name: {cfg.parent!r}"
    if not (1 <= cfg.vlan_id <= 4094):
        return f"VLAN id out of range (1-4094): {cfg.vlan_id}"
    # Reuse the iface-cfg validator for the IP/DNS/MTU bits — minus
    # the iface-name check we just did, since the VLAN name has
    # different rules (dot allowed) which _valid_iface_name accepts.
    if not cfg.dhcp4 and not cfg.addresses4:
        return "static VLAN requires at least one address (CIDR form)"
    err = _validate_addresses(cfg.addresses4)
    if err:
        return err
    if cfg.gateway4 and not _valid_ipv4(cfg.gateway4):
        return f"invalid IPv4 gateway: {cfg.gateway4!r}"
    for d in cfg.dns:
        if not _valid_ipv4(d):
            return f"invalid DNS address: {d!r}"
    if cfg.mtu and not (576 <= cfg.mtu <= 9216):
        return f"MTU out of range (576-9216): {cfg.mtu}"
    if not (30 <= cfg.timeout_s <= 600):
        return f"timeout out of range (30-600s): {cfg.timeout_s}"
    return None


# Back-compat shim — older tests still reference _validate_config
# under the slice-3b name. Routes to the iface validator.
_validate_config = _validate_iface_cfg


@router.get("/interfaces/pending", response_model=PendingApply)
async def get_pending_apply() -> PendingApply:
    """Is a netplan apply still inside its auto-revert window?

    Polled by the UI countdown so the operator can see how many
    seconds are left before rollback. Reads /run/phonon-net/pending.json
    directly — no sudo needed, the helper writes it world-readable
    because we want this path to stay cheap."""
    import time
    p = Path(_PENDING_STATE_FILE)
    if not p.exists():
        return PendingApply(pending=False)
    try:
        data = json.loads(p.read_text())
        expires = int(data.get("expires_at", 0))
        return PendingApply(
            pending=True,
            expires_at=expires,
            backup_dir=str(data.get("backup_dir", "")),
            seconds_remaining=max(0, expires - int(time.time())),
        )
    except Exception:
        # Corrupt pending file — report as pending so the UI shows
        # an indeterminate countdown rather than hiding the timer.
        return PendingApply(pending=True)


async def _apply_state(timeout_s: int) -> ApplyResult:
    """Render the current state to YAML, invoke the helper with the
    given rollback window, and return the result. Single place that
    does helper invocation so PUT/POST/DELETE all behave the same."""
    body = render_netplan_yaml(_state)
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", _PHONON_NET_BIN, "apply-iface", str(timeout_s),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(body.encode("utf-8")), timeout=30.0
        )
        rc = proc.returncode if proc.returncode is not None else -1
        out = out_b.decode("utf-8", errors="replace")
        err_out = err_b.decode("utf-8", errors="replace")
    except Exception as exc:
        return ApplyResult(ok=False, message=f"helper invocation failed: {exc}")
    if rc != 0:
        msg = (err_out or out).strip()[:300] or "helper failed"
        return ApplyResult(ok=False, message=f"rc={rc}: {msg}")
    pending = await get_pending_apply()
    return ApplyResult(
        ok=True,
        message=f"Applied. Rollback in {timeout_s}s if not confirmed.",
        expires_at=pending.expires_at,
    )


@router.get("/managed", response_model=NetworkState)
async def get_managed_state() -> NetworkState:
    """Return the phonon-managed iface + VLAN overrides. Anything
    not in here falls back to the distro / installer-supplied
    netplan defaults."""
    return _state


@router.put("/interfaces/{name}", response_model=ApplyResult)
async def apply_iface_config(name: str, cfg: IfaceConfig) -> ApplyResult:
    """Apply a new netplan config to one iface (DHCP/static, primary
    + aliases, gateway, DNS, MTU) with an auto-revert window.

    Idempotent on a pending apply — if a prior pending apply exists,
    the helper rolls it back BEFORE applying the new one. Otherwise
    the new "backup" would capture the about-to-be-undone state.
    """
    err = _validate_iface_cfg(name, cfg)
    if err:
        return ApplyResult(ok=False, message=err)
    # Stage the change in memory first, then render+apply.
    prev = _state.ethernets.get(name)
    _state.ethernets[name] = cfg
    _save_state()
    result = await _apply_state(cfg.timeout_s)
    if not result.ok:
        # Roll the in-memory state back so a failed apply doesn't
        # leave us with state.json out of sync with what's on disk.
        if prev is None:
            _state.ethernets.pop(name, None)
        else:
            _state.ethernets[name] = prev
        _save_state()
    return result


@router.delete("/interfaces/{name}", response_model=ApplyResult)
async def delete_iface_override(name: str, timeout_s: int = 120) -> ApplyResult:
    """Drop the phonon-managed override for this iface so it falls
    back to the distro defaults (typically DHCP from the installer
    config). Same auto-revert window as PUT."""
    if name not in _state.ethernets:
        return ApplyResult(ok=False, message=f"no managed override for {name!r}")
    if not (30 <= timeout_s <= 600):
        return ApplyResult(ok=False, message="timeout out of range (30-600s)")
    prev = _state.ethernets.pop(name)
    _save_state()
    result = await _apply_state(timeout_s)
    if not result.ok:
        _state.ethernets[name] = prev
        _save_state()
    return result


@router.post("/vlans", response_model=ApplyResult)
async def create_vlan(cfg: VlanConfig) -> ApplyResult:
    """Create a VLAN-tagged child interface. The child name is
    derived as `<parent>.<vlan_id>` — netplan's canonical form, also
    matches what `ip link add` produces by default."""
    name = f"{cfg.parent}.{cfg.vlan_id}"
    err = _validate_vlan_cfg(name, cfg)
    if err:
        return ApplyResult(ok=False, message=err)
    if name in _state.vlans:
        return ApplyResult(ok=False, message=f"VLAN {name} already managed (use PUT to update)")
    _state.vlans[name] = cfg
    _save_state()
    result = await _apply_state(cfg.timeout_s)
    if not result.ok:
        _state.vlans.pop(name, None)
        _save_state()
    return result


@router.put("/vlans/{name}", response_model=ApplyResult)
async def update_vlan(name: str, cfg: VlanConfig) -> ApplyResult:
    """Update an existing VLAN's IP / DNS / MTU. The name must match
    the existing entry — to change parent or vlan_id, DELETE + POST
    a fresh one (renaming a netplan child is a destructive op)."""
    if name not in _state.vlans:
        return ApplyResult(ok=False, message=f"VLAN {name!r} not found")
    expected = f"{cfg.parent}.{cfg.vlan_id}"
    if expected != name:
        return ApplyResult(
            ok=False,
            message=f"cannot rename VLAN ({expected} ≠ {name}) — delete and recreate instead",
        )
    err = _validate_vlan_cfg(name, cfg)
    if err:
        return ApplyResult(ok=False, message=err)
    prev = _state.vlans[name]
    _state.vlans[name] = cfg
    _save_state()
    result = await _apply_state(cfg.timeout_s)
    if not result.ok:
        _state.vlans[name] = prev
        _save_state()
    return result


@router.delete("/vlans/{name}", response_model=ApplyResult)
async def delete_vlan(name: str, timeout_s: int = 120) -> ApplyResult:
    """Remove a phonon-managed VLAN child. Triggers netplan reload
    so the kernel-level vlan device is destroyed."""
    if name not in _state.vlans:
        return ApplyResult(ok=False, message=f"VLAN {name!r} not found")
    if not (30 <= timeout_s <= 600):
        return ApplyResult(ok=False, message="timeout out of range (30-600s)")
    prev = _state.vlans.pop(name)
    _save_state()
    result = await _apply_state(timeout_s)
    if not result.ok:
        _state.vlans[name] = prev
        _save_state()
    return result


@router.post("/interfaces/confirm", response_model=ApplyResult)
async def confirm_apply() -> ApplyResult:
    """Make the pending apply permanent — cancels the rollback timer
    and drops the backup."""
    rc, out, err = await _run(
        ["sudo", "-n", _PHONON_NET_BIN, "confirm"], timeout=10.0
    )
    if rc != 0:
        return ApplyResult(ok=False, message=f"rc={rc}: {(err or out).strip()[:200]}")
    return ApplyResult(ok=True, message="Config confirmed.")


@router.post("/interfaces/cancel", response_model=ApplyResult)
async def cancel_apply() -> ApplyResult:
    """Roll back the pending apply right now. Used for "Revert now"."""
    rc, out, err = await _run(
        ["sudo", "-n", _PHONON_NET_BIN, "cancel"], timeout=30.0
    )
    if rc != 0:
        return ApplyResult(ok=False, message=f"rc={rc}: {(err or out).strip()[:200]}")
    return ApplyResult(ok=True, message="Rolled back.")


@router.get("/interfaces", response_model=list[IfaceInfo])
async def list_interfaces_detailed(request: Request) -> list[IfaceInfo]:
    """Per-interface snapshot used by the Network tab's iface table.

    Pulls `ip -j addr` + `ip -j route` for the kernel view and
    `networkctl status <iface>` for the systemd-networkd / netplan
    view. The current request's local socket is matched to one
    interface and flagged `is_mgmt=True` so the UI can protect it
    from edits in later slices.
    """
    nds = await _ip_addr_json()
    routes = await _ip_route_json()
    mgmt = _detect_mgmt_iface(request)
    out: list[IfaceInfo] = []
    # Skip loopback for the UI table — never editable, never useful here.
    for nd in nds:
        if nd.get("link_type") == "loopback":
            continue
        try:
            out.append(await _build_iface_info(nd, routes, mgmt))
        except Exception:
            logger.warning("network.iface_info_failed", iface=nd.get("ifname"), exc_info=True)
    return out
