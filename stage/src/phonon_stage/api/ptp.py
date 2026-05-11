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
import contextlib
import re
import shutil
from pathlib import Path
from typing import Any

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
    # Grandmaster identity from PARENT_DATA_SET (e.g. "abcdef.fffe.123456").
    # Empty when role==grandmaster (we ARE the GM, nothing to report) or
    # when the BMCA hasn't elected one yet. For a slave node this is the
    # ID of whoever Phonon is syncing TO.
    grandmaster_id: str = ""
    # Convenience: "self" if we're the GM, "<id>" if we're tracking a remote,
    # "—" if BMCA pending. UI displays this directly.
    grandmaster_label: str = ""
    # Hardware-timestamping capability of the bound interface, detected
    # via `ethtool -T`. UI uses this to grey out the 'hardware' option in
    # the time_stamping dropdown when the NIC can't deliver it (Realtek
    # onboard, USB-Ethernet on Pi).
    hw_timestamping_supported: bool = False
    # Resolved time_stamping mode actually in use. When Settings.time_stamping
    # = 'auto', this is what the renderer picked (hardware or software).
    # Useful for the UI to tell the user "you asked for auto and got
    # hardware" without re-doing the detection client-side.
    effective_time_stamping: str = ""


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
        "pgrep",
        "-x",
        "ptp4l",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    rc = await proc.wait()
    return rc == 0


async def _ptp4l_interface() -> str:
    """Extract the -i argument from the running ptp4l's command line."""
    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-C",
        "ptp4l",
        "-o",
        "args=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="ignore")
    m = re.search(r"-i\s+(\S+)", text)
    return m.group(1) if m else ""


async def _query_pmc_state() -> tuple[str, int | None]:
    """Ask ptp4l directly via the PTP Management Client (pmc).

    Way more reliable than scraping the journal: a node running stably
    as MASTER for hours emits no log lines (only the original transition,
    which has already rolled out), so the journal-based parser fell back
    to 'unknown'. pmc returns the live `portState` of port 1, regardless
    of how long the daemon has been running.

    Two queries:
      * PORT_DATA_SET → portState (LISTENING / MASTER / SLAVE …)
      * CURRENT_DATA_SET → meanPathDelay + offsetFromMaster (slave only)

    Both pmc invocations run via sudo because pmc binds to a root-owned
    UDS socket exposed by ptp4l. The NOPASSWD grant is scoped exactly
    to these two read-only queries by install.sh.
    """
    role = "unknown"
    offset_ns: int | None = None
    # Routed through the /usr/local/sbin/phonon-ptp-query wrapper so the
    # sudoers grant is on a no-arg-quirk script (NOPASSWD: phonon-ptp-query
    # port). Direct `sudo /usr/sbin/pmc -u -b 0 "GET PORT_DATA_SET"` runs
    # into sudoers/argv tokenizer issues with the quoted command token —
    # the wrapper sidesteps that entirely.
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        "/usr/local/sbin/phonon-ptp-query",
        "port",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except TimeoutError:
        proc.kill()
        return role, offset_ns
    if proc.returncode != 0:
        return role, offset_ns
    text = out.decode(errors="ignore")
    m = re.search(r"portState\s+(\w+)", text)
    if m:
        s = m.group(1).upper()
        if s == "MASTER":
            role = "grandmaster"
        elif s in ("SLAVE", "UNCALIBRATED"):
            role = "slave"
        elif s == "LISTENING":
            role = "listening"

    # Pull current offset for slave nodes
    if role == "slave":
        proc2 = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "/usr/local/sbin/phonon-ptp-query",
            "current",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=2.0)
            if proc2.returncode == 0:
                m2 = re.search(r"offsetFromMaster\s+(-?\d+)", out2.decode(errors="ignore"))
                if m2:
                    offset_ns = int(m2.group(1))
        except TimeoutError:
            proc2.kill()
    return role, offset_ns


async def _query_pmc_parent() -> str:
    """Query `pmc GET PARENT_DATA_SET` and extract the grandmaster identity.

    Output of pmc on parent dataset includes (among others):
        gm.ClockIdentity        abcdef.fffe.123456
        grandmasterIdentity     abcdef.fffe.123456
    Different ptp4l versions use slightly different keys — try both. The
    clock identity is 8 hex bytes formatted as `XXXX.XXXX.XXXXXXXX` or
    a dotted variant; we normalize to lowercase with dots for display.

    Empty string if pmc fails or we're not running ptp4l.
    """
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        "/usr/local/sbin/phonon-ptp-query",
        "parent",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except TimeoutError:
        proc.kill()
        return ""
    if proc.returncode != 0:
        return ""
    text = out.decode(errors="ignore")
    # Try the canonical key first ('grandmasterIdentity') then a couple of
    # alternates that ptp4l emits depending on the build. The value is a
    # ClockIdentity (8 bytes), formatted by ptp4l as XX:XX:XX.FF:FE.XX:XX:XX
    # or XXXX.XXXX.XXXXXXXX. Capture the whole token after the key.
    for key in ("grandmasterIdentity", "gm.ClockIdentity", "clockIdentity"):
        m = re.search(rf"{key}\s+(\S+)", text)
        if m:
            return m.group(1).lower()
    return ""


async def _read_journal_state() -> tuple[str, int | None]:
    """Try to extract role + offset from the most recent ptp4l log lines.

    Use `-t ptp4l` (syslog identifier) which matches both manually-launched
    ptp4l and the phonon-ptp4l systemd service — `-u` would only see the
    service variant.
    """
    # Pull a much larger window than 50 lines: PMC subscribers (one per
    # /ptp/status hit) produce ~12 'subscriber timed out' lines per minute
    # that quickly evict the actual state-transition logs in any short tail.
    # 2000 lines covers ~2-3 hours of subscriber spam plus the boot lines.
    proc = await asyncio.create_subprocess_exec(
        "journalctl",
        "-t",
        "ptp4l",
        "-n",
        "2000",
        "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    raw = out.decode(errors="ignore")
    if not raw.strip():
        return "unknown", None
    # Drop the noisy PMC subscriber lines — they are not state transitions
    # and would otherwise crowd out the role markers we actually care about.
    text = "\n".join(
        line for line in raw.splitlines() if "subscriber" not in line and "timed out" not in line
    )
    if not text.strip():
        return "unknown", None
    role = "unknown"
    offset: int | None = None
    # Walk the log forwards and keep updating role on every state-change
    # marker — only the LATEST event reflects the current role. ptp4l
    # often elects itself master first, then sees a peer with a better
    # clock identity and demotes to slave; if we used 'first match'
    # semantics we'd permanently report grandmaster on a slave node.
    state_markers = (
        # (regex, role)
        (re.compile(r"port \d+.*\bto SLAVE\b"), "slave"),
        (re.compile(r"port \d+.*\bto MASTER\b"), "grandmaster"),
        (re.compile(r"port \d+.*\bto LISTENING\b"), "listening"),
        (re.compile(r"port \d+.*: INITIALIZING to LISTENING"), "listening"),
        (re.compile(r"selected local clock"), "grandmaster"),
        (re.compile(r"assuming the grand master role"), "grandmaster"),
        # ptp4l in SLAVE state emits periodic 'rms ... max ... freq ... delay'
        # stats. After the journal has rolled past the original LISTENING→
        # SLAVE transition, those stats are the only surviving evidence
        # that this node is acting as a slave.
        (re.compile(r"\brms\s+\d+\s+max\s+\d+\s+freq"), "slave"),
        # 'master offset' lines also imply SLAVE (only slaves report it).
        (re.compile(r"\bmaster offset\s+-?\d+"), "slave"),
    )
    for line in text.splitlines():
        for pattern, candidate in state_markers:
            if pattern.search(line):
                role = candidate
                break
    # Pick the LAST master-offset reading so the displayed offset is current
    # (slaves emit one per sync interval).
    last_offset = None
    for m in re.finditer(r"master offset\s+(-?\d+)", text):
        last_offset = int(m.group(1))
    if last_offset is not None:
        offset = last_offset
    return role, offset


PTP4L_SERVICE = "phonon-ptp4l.service"
PHC2SYS_SERVICE = "phonon-phc2sys.service"


async def _systemctl(*args: str) -> tuple[int, str]:
    """Run systemctl via sudo (NOPASSWD setup by install.sh). Returns (rc, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        "/bin/systemctl",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    return proc.returncode or 0, stderr_bytes.decode(errors="ignore")


async def _service_unit_exists(name: str) -> bool:
    """Check unit existence without sudo so we don't conflate sudo failures
    with unit absence. Uses list-unit-files which is world-readable."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/systemctl",
        "list-unit-files",
        "--no-legend",
        "--no-pager",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out_bytes, _ = await proc.communicate()
    return name.encode() in out_bytes


async def _service_active(name: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "/bin/systemctl",
        "is-active",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out_bytes, _ = await proc.communicate()
    return out_bytes.decode().strip() == "active"


PTP_CONF_PATH = Path("/etc/linuxptp/phonon-aes67.conf")


async def detect_hw_timestamping(iface: str) -> bool:
    """Return True iff `iface` supports hardware PTP timestamping.

    Inspects `ethtool -T <iface>` and looks for the SOF_TIMESTAMPING_TX_HARDWARE
    + SOF_TIMESTAMPING_RX_HARDWARE capability flags AND a PHC index ≥ 0.
    Both together prove the NIC has the dedicated hardware clock that ptp4l
    needs for sub-microsecond sync. Falls back to False on Realtek/USB-Eth
    where everything's done by the kernel (software timestamping, ~50 µs
    RMS at best).
    """
    if not iface:
        return False
    proc = await asyncio.create_subprocess_exec(
        "ethtool",
        "-T",
        iface,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except TimeoutError:
        proc.kill()
        return False
    if proc.returncode != 0:
        return False
    text = out.decode(errors="ignore")
    has_hw_tx = "hardware-transmit" in text or "SOF_TIMESTAMPING_TX_HARDWARE" in text
    has_hw_rx = "hardware-receive" in text or "SOF_TIMESTAMPING_RX_HARDWARE" in text
    # PHC line looks like "PTP Hardware Clock: N" where N >= 0 means a clock
    # exists. "none" or absent = no PHC.
    has_phc = False
    m = re.search(r"PTP Hardware Clock:\s*(\S+)", text)
    if m:
        v = m.group(1)
        has_phc = v.isdigit() and int(v) >= 0
    return has_hw_tx and has_hw_rx and has_phc


def _render_ptp4l_conf(cfg: Any) -> str:
    """Build /etc/linuxptp/phonon-aes67.conf text from PtpSettings.

    `cfg` is settings.PtpSettings; passed as Any to avoid an import cycle
    with the settings module (which itself ends up importing this).
    """
    # Mode flags. ptp4l 4.x supports `serverOnly` for forced-master; older
    # builds don't, but the priority1=1 fallback in 'grandmaster' mode
    # accomplishes nearly the same thing.
    server_only = "1" if cfg.mode == "grandmaster" else "0"
    slave_only = "1" if cfg.mode == "slave" else "0"

    lines = [
        "# Auto-generated by phonon-stage — edit Settings.ptp via UI/API.",
        "# Manual edits will be overwritten on the next settings PATCH.",
        "",
        "[global]",
        f"domainNumber              {cfg.domain}",
        f"priority1                 {cfg.priority1}",
        f"priority2                 {cfg.priority2}",
        f"clockClass                {cfg.clock_class}",
        f"clockAccuracy             {cfg.clock_accuracy.lower()}",
        f"offsetScaledLogVariance   {cfg.offset_scaled_log_variance.lower()}",
        f"slaveOnly                 {slave_only}",
        # serverOnly is silently ignored on linuxptp <4.0 — harmless.
        f"serverOnly                {server_only}",
        "free_running              0",
        "freq_est_interval         1",
        f"dscp_event                {cfg.dscp_event}",
        f"dscp_general              {cfg.dscp_general}",
        f"network_transport         {cfg.network_transport}",
        f"delay_mechanism           {cfg.delay_mechanism}",
        f"time_stamping             {cfg.time_stamping if cfg.time_stamping != 'auto' else 'software'}",  # noqa: E501
        f"tx_timestamp_timeout      {cfg.tx_timestamp_timeout}",
        f"logAnnounceInterval       {cfg.log_announce_interval}",
        f"logSyncInterval           {cfg.log_sync_interval}",
        f"logMinDelayReqInterval    {cfg.log_min_delay_req_interval}",
        f"announceReceiptTimeout    {cfg.announce_receipt_timeout}",
        f"hybrid_e2e                {'1' if cfg.hybrid_e2e else '0'}",
        f"inhibit_multicast_service {'1' if cfg.inhibit_multicast_service else '0'}",
        f"clock_servo               {cfg.clock_servo}",
        f"step_threshold            {cfg.step_threshold}",
        f"first_step_threshold      {cfg.first_step_threshold}",
        f"max_frequency             {cfg.max_frequency}",
    ]
    return "\n".join(lines) + "\n"


async def _write_ptp4l_conf(cfg: Any) -> bool:
    """Render config from settings and write to /etc/linuxptp/phonon-aes67.conf.

    Returns True iff the file changed (signal to caller that a service
    restart is needed). install.sh chown's the file to phonon:phonon so
    no sudo needed for the write — atomic via tmp + rename.
    """
    # Auto-detect hardware timestamping when settings.time_stamping is 'auto'.
    # We mutate a shallow copy of cfg so we don't side-effect the live
    # Settings object; the on-disk JSON stays at 'auto' regardless.
    effective = cfg.model_copy()
    if effective.time_stamping == "auto":
        iface = effective.interface or await _ptp4l_interface()
        effective.time_stamping = "hardware" if await detect_hw_timestamping(iface) else "software"

    new_text = _render_ptp4l_conf(effective)
    old_text = ""
    if PTP_CONF_PATH.exists():
        with contextlib.suppress(OSError):
            old_text = PTP_CONF_PATH.read_text()
    if new_text == old_text:
        return False
    tmp = PTP_CONF_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(new_text)
        tmp.replace(PTP_CONF_PATH)
    except OSError:
        logger.warning("ptp.conf_write_failed", path=str(PTP_CONF_PATH), exc_info=True)
        return False
    logger.info("ptp.conf_written", path=str(PTP_CONF_PATH), bytes=len(new_text))
    return True


async def apply_settings() -> None:
    """Reconcile ptp4l/phc2sys services with the current Settings.ptp.

    Called from /settings PATCH. The systemd units must have been
    installed by deploy/install.sh — if they're absent we skip silently
    so dev boxes without the install don't crash. The user's NOPASSWD
    sudoers entry is also set up by install.sh.

    Steps:
      1. Render PtpSettings → /etc/linuxptp/phonon-aes67.conf.
      2. If config text changed, restart ptp4l (+ phc2sys) to pick it up.
      3. Reconcile enable/disable state with cfg.enabled.
    """
    from phonon_stage.api import settings as _settings_mod

    cfg = _settings_mod.get().ptp
    if not await _service_unit_exists(PTP4L_SERVICE):
        logger.info("ptp.apply_skipped", reason="unit_not_installed")
        return

    # Step 1+2: render conf + restart on change. Only do this when ptp4l
    # is expected to be running; if cfg.enabled=False, disabling the
    # service later in step 3 makes a conf-change-induced restart moot.
    conf_changed = False
    if cfg.enabled:
        conf_changed = await _write_ptp4l_conf(cfg)

    if cfg.enabled:
        already_active = await _service_active(PTP4L_SERVICE)
        running_iface = await _ptp4l_interface() if already_active else ""
        wants_iface = cfg.interface

        iface_changed = already_active and wants_iface != "" and wants_iface != running_iface

        if already_active and (conf_changed or iface_changed):
            rc, err = await _systemctl("restart", PTP4L_SERVICE)
            await _systemctl("restart", PHC2SYS_SERVICE)
            logger.info(
                "ptp.services_restarted",
                reason="conf_changed" if conf_changed else "iface_changed",
                from_iface=running_iface,
                to_iface=wants_iface,
                rc=rc,
                err=err.strip(),
            )
        else:
            rc1, e1 = await _systemctl("enable", "--now", PTP4L_SERVICE)
            if cfg.phc2sys_enabled:
                rc2, e2 = await _systemctl("enable", "--now", PHC2SYS_SERVICE)
            else:
                rc2, e2 = await _systemctl("disable", "--now", PHC2SYS_SERVICE)
            logger.info(
                "ptp.services_enabled",
                ptp4l_rc=rc1,
                phc2sys_rc=rc2,
                ptp4l_err=e1.strip(),
                phc2sys_err=e2.strip(),
            )
    else:
        rc1, e1 = await _systemctl("disable", "--now", PTP4L_SERVICE)
        rc2, e2 = await _systemctl("disable", "--now", PHC2SYS_SERVICE)
        logger.info(
            "ptp.services_disabled",
            ptp4l_rc=rc1,
            phc2sys_rc=rc2,
            ptp4l_err=e1.strip(),
            phc2sys_err=e2.strip(),
        )


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
            note = (
                "Settings say enabled, but phonon-ptp4l.service "
                "not installed — run deploy/install.sh"
            )
        elif cfg.enabled:
            note = (
                "Settings say enabled, but ptp4l is not running — "
                "check `journalctl -u phonon-ptp4l`"
            )
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
    # Prefer pmc (live ptp4l Unix-domain query) — works even when the
    # journal has rolled past the original state transitions. Fall back
    # to the journal parser if pmc is missing or the sudoers grant
    # hasn't been applied yet.
    role, offset_ns = await _query_pmc_state()
    if role == "unknown":
        role, offset_ns = await _read_journal_state()
    via_unit = await _service_active(PTP4L_SERVICE)
    iface = await _ptp4l_interface()

    # Grandmaster identity — only meaningful for a slave (we're tracking
    # someone) or listening (BMCA in progress). When we're the GM ourselves,
    # the parent dataset reports our own clockId; rather than expose that
    # confusing detail we just say "self" so the UI is unambiguous.
    gm_id = ""
    gm_label = ""
    if role == "grandmaster":
        gm_label = "self"
    elif role in ("slave", "listening"):
        gm_id = await _query_pmc_parent()
        gm_label = gm_id if gm_id else "—"

    if via_unit:
        note = "managed by phonon-ptp4l.service"
        # Detect mismatch between Settings and reality
        if cfg.interface and iface and cfg.interface != iface:
            note = (
                f"running on '{iface}' but Settings.interface='{cfg.interface}' — restart to apply"
            )
    else:
        note = "ptp4l running (manually launched, not via systemd unit)"

    # Hardware-timestamping capability — useful for the UI to grey out the
    # 'hardware' option if the running interface can't deliver it. Detection
    # is cheap (`ethtool -T` returns immediately).
    hw_ts_ok = await detect_hw_timestamping(iface)

    # Resolved effective time_stamping mode.
    if cfg.time_stamping == "auto":
        effective_ts = "hardware" if hw_ts_ok else "software"
    else:
        effective_ts = cfg.time_stamping

    return PtpStatus(
        available=True,
        running=True,
        role=role,
        offset_ns=offset_ns,
        interface=iface,
        profile=cfg.profile,
        note=note,
        grandmaster_id=gm_id,
        grandmaster_label=gm_label,
        hw_timestamping_supported=hw_ts_ok,
        effective_time_stamping=effective_ts,
    )
