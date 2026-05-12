"""Network endpoints — NTP/clock status, interface info, QoS marking.

Phase 1:
  * slice 1: read-only NTP status panel
  * slice 2: edit NTP server list + manual sync
  * slice 3a: read-only interface table ← this commit
  * slice 3b: edit interface IP / DHCP / static via netplan try
  * slice 4: VLAN tagged iface + IP aliases via netplan
  * slice 5: WiFi scan + connect (netplan or wpa_cli)
  * slice 6: PTP interface binding UI
  * slice 7: DSCP marking for AES67/PTP via nftables

Write paths use sudoers-granted scripts (`/usr/local/sbin/phonon-ntp`,
`phonon-net`) so we don't run the API server as root. netplan write
paths use `netplan try` with a 120-second auto-revert window to
make the mgmt iface effectively un-brickable.
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
