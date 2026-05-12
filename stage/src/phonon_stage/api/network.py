"""Network endpoints — NTP/clock status, QoS marking, etc.

Phase 1:
  * slice 1: read-only NTP status panel
  * slice 2: edit NTP server list + manual sync ← this commit
  * slice 3+: interface IP / VLAN / WiFi (TBD on backend)
  * slice 6: DSCP marking for AES67/PTP via nftables

Write paths use sudoers-granted scripts (`/usr/local/sbin/phonon-ntp`)
so we don't run the API server as root. Each script does ONE thing
+ atomic file replace + daemon restart, so a failure can't half-write
the chrony config.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter
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
