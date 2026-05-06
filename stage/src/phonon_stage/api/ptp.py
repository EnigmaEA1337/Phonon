"""PTP (IEEE 1588) status — scaffold.

The Stage doesn't manage ptp4l itself yet. This module just detects
whether linuxptp is installed and whether ptp4l is currently running,
and exposes the offset/state via the management socket if available.

When the Stage starts being deployed on wired networks for real
multi-machine AES67, we'll grow this into a full integration:
  * config flags (mode=auto/grandmaster/slave, interface, profile)
  * spawn ptp4l + phc2sys with the right flags
  * watch their state and surface degradation as alerts

For now: read-only, best-effort, no root privileges required.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/ptp", tags=["ptp"])
logger = structlog.get_logger()


class PtpStatus(BaseModel):
    """Snapshot of the local PTP state."""

    available: bool
    running: bool
    role: str  # "grandmaster", "slave", "listening", "unknown", "n/a"
    offset_ns: int | None
    interface: str
    profile: str
    note: str


PTP4L_BINS = ("/usr/sbin/ptp4l", "/usr/bin/ptp4l")
PHC2SYS_BINS = ("/usr/sbin/phc2sys", "/usr/bin/phc2sys")


def _ptp4l_installed() -> str:
    for p in PTP4L_BINS:
        if Path(p).is_file():
            return p
    found = shutil.which("ptp4l")
    return found or ""


async def _ptp4l_running() -> bool:
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-x", "ptp4l",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    return rc == 0


async def _read_journal_state() -> tuple[str, int | None]:
    """Try to extract role + offset from the most recent ptp4l log lines.

    Use `-t ptp4l` (syslog identifier) which matches both manually-launched
    ptp4l and the phonon-ptp4l systemd service — `-u` would only see the
    service variant.
    """
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-t", "ptp4l", "-n", "50", "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="ignore")
    if not text.strip():
        return "unknown", None
    role = "unknown"
    offset: int | None = None
    # ptp4l logs lines like:
    #   "selected best master clock ..."
    #   "port 1: MASTER to LISTENING"
    #   "master offset       1234 s2 freq ..."
    if "selected local clock" in text:
        role = "grandmaster"
    elif re.search(r"port \d+: SLAVE", text):
        role = "slave"
    elif re.search(r"port \d+: MASTER", text):
        role = "grandmaster"
    elif re.search(r"port \d+: LISTENING", text):
        role = "listening"
    m = re.search(r"master offset\s+(-?\d+)", text)
    if m:
        offset = int(m.group(1))
    return role, offset


PTP4L_SERVICE = "phonon-ptp4l.service"
PHC2SYS_SERVICE = "phonon-phc2sys.service"


async def _systemctl(*args: str) -> tuple[int, str]:
    """Run systemctl via sudo (NOPASSWD setup by install.sh). Returns (rc, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "/bin/systemctl", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    return proc.returncode or 0, stderr_bytes.decode(errors="ignore")


async def _service_unit_exists(name: str) -> bool:
    """Check unit existence without sudo so we don't conflate sudo failures
    with unit absence. Uses list-unit-files which is world-readable."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/systemctl", "list-unit-files", "--no-legend", "--no-pager", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out_bytes, _ = await proc.communicate()
    return name.encode() in out_bytes


async def _service_active(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "/bin/systemctl", "is-active", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out_bytes, _ = await proc.communicate()
    return out_bytes.decode().strip() == "active"


async def apply_settings() -> None:
    """Reconcile ptp4l/phc2sys services with the current Settings.ptp.

    Called from /settings PATCH. The systemd units must have been
    installed by deploy/install.sh — if they're absent we skip silently
    so dev boxes without the install don't crash. The user's NOPASSWD
    sudoers entry is also set up by install.sh.
    """
    from phonon_stage.api import settings as _settings_mod

    cfg = _settings_mod.get().ptp
    if not await _service_unit_exists(PTP4L_SERVICE):
        logger.info("ptp.apply_skipped", reason="unit_not_installed")
        return

    if cfg.enabled:
        rc1, e1 = await _systemctl("enable", "--now", PTP4L_SERVICE)
        rc2, e2 = await _systemctl("enable", "--now", PHC2SYS_SERVICE)
        logger.info("ptp.services_enabled", ptp4l_rc=rc1, phc2sys_rc=rc2,
                    ptp4l_err=e1.strip(), phc2sys_err=e2.strip())
    else:
        rc1, e1 = await _systemctl("disable", "--now", PTP4L_SERVICE)
        rc2, e2 = await _systemctl("disable", "--now", PHC2SYS_SERVICE)
        logger.info("ptp.services_disabled", ptp4l_rc=rc1, phc2sys_rc=rc2,
                    ptp4l_err=e1.strip(), phc2sys_err=e2.strip())


@router.get("/status", response_model=PtpStatus)
async def ptp_status() -> PtpStatus:
    from phonon_stage.api import settings as _settings_mod

    cfg = _settings_mod.get().ptp
    bin_path = _ptp4l_installed()
    if not bin_path:
        return PtpStatus(
            available=False,
            running=False,
            role="n/a",
            offset_ns=None,
            interface="",
            profile="",
            note="linuxptp not installed (apt install linuxptp)",
        )
    unit_present = await _service_unit_exists(PTP4L_SERVICE)
    running = await _ptp4l_running()
    if not running:
        if cfg.enabled and not unit_present:
            note = "Settings say enabled, but phonon-ptp4l.service not installed — run deploy/install.sh"
        elif cfg.enabled:
            note = "Settings say enabled, but ptp4l is not running — check `journalctl -u phonon-ptp4l`"
        else:
            note = f"{bin_path} present, disabled in Settings"
        return PtpStatus(
            available=True,
            running=False,
            role="n/a",
            offset_ns=None,
            interface="",
            profile="",
            note=note,
        )
    role, offset_ns = await _read_journal_state()
    via_unit = await _service_active(PTP4L_SERVICE)
    if via_unit:
        note = "managed by phonon-ptp4l.service"
    else:
        note = "ptp4l running (manually launched, not via systemd unit)"
    return PtpStatus(
        available=True,
        running=True,
        role=role,
        offset_ns=offset_ns,
        interface="",  # TODO: parse from ps output
        profile=cfg.profile,
        note=note,
    )
