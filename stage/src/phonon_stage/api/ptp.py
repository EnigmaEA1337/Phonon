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

    Looks at journalctl (no root needed for read on most distros) or
    /var/log/syslog as a fallback. Returns (role, offset_ns).
    """
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-u", "ptp4l", "-n", "30", "--no-pager",
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


@router.get("/status", response_model=PtpStatus)
async def ptp_status() -> PtpStatus:
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
    running = await _ptp4l_running()
    if not running:
        return PtpStatus(
            available=True,
            running=False,
            role="n/a",
            offset_ns=None,
            interface="",
            profile="",
            note=f"{bin_path} present but not running",
        )
    role, offset_ns = await _read_journal_state()
    return PtpStatus(
        available=True,
        running=True,
        role=role,
        offset_ns=offset_ns,
        interface="",  # TODO: parse from ps output
        profile="default",
        note="read-only scaffold; Stage does not manage ptp4l yet",
    )
