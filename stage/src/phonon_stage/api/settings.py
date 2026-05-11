"""Stage settings — runtime-tunable knobs persisted to JSON.

Defaults are AES67-spec-aligned and chosen for a typical 2-Stage dev
setup. Everything is editable via PATCH /settings.

Persistence file: <data_dir>/settings.json (mode 0600).
"""

from __future__ import annotations

import contextlib
import os
import socket
from pathlib import Path
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/settings", tags=["settings"])
logger = structlog.get_logger()


class SapSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    announce_enabled: bool = True
    announce_interval_s: int = Field(default=10, ge=2, le=60)
    listen_enabled: bool = True


class PtpSettings(BaseModel):
    """All ptp4l(8) parameters surfaced for UI control. The full
    /etc/linuxptp/phonon-aes67.conf is rendered from this model — every
    field maps to one line of the [global] section. Defaults match the
    AES67 Media Profile §6 (sub-millisecond, multicast, 8 Hz announce
    + sync). Snake_case here, the renderer converts to ptp4l's
    camelCase / mixed-case keys.
    """

    model_config = ConfigDict(extra="forbid")

    # — Activation —
    enabled: bool = False
    interface: str = ""  # empty = auto-pick first wired NIC up
    profile: Literal["aes67", "smpte2059-2", "default"] = "aes67"
    mode: Literal["auto", "grandmaster", "slave"] = "auto"
    # auto        : full BMCA election, can be either role
    # grandmaster : refuse to become slave (serverOnly=1 on linuxptp ≥4.0,
    #               or priority1=1 fallback on older builds)
    # slave       : refuse to become master (slaveOnly=1)

    # — BMCA / clock identity —
    priority1: int = Field(default=128, ge=0, le=255)
    priority2: int = Field(default=128, ge=0, le=255)
    clock_class: int = Field(default=248, ge=0, le=255)
    clock_accuracy: str = Field(default="0xFE", pattern=r"^0x[0-9A-Fa-f]{2}$")
    offset_scaled_log_variance: str = Field(default="0xFFFF", pattern=r"^0x[0-9A-Fa-f]{4}$")
    domain: int = Field(default=0, ge=0, le=127)

    # — Timing (log2 seconds for *_interval fields) —
    log_announce_interval: int = Field(default=1, ge=-3, le=4)
    log_sync_interval: int = Field(default=-3, ge=-7, le=4)
    log_min_delay_req_interval: int = Field(default=-3, ge=-7, le=4)
    announce_receipt_timeout: int = Field(default=3, ge=2, le=10)

    # — Transport —
    network_transport: Literal["UDPv4", "UDPv6", "L2"] = "UDPv4"
    delay_mechanism: Literal["E2E", "P2P"] = "E2E"
    # 'auto' = backend picks hardware if ethtool -T reports SOF_TIMESTAMPING_TX_HARDWARE,
    # falls back to software. Manual override useful for testing on HW-capable cards.
    time_stamping: Literal["auto", "hardware", "software", "legacy"] = "auto"
    tx_timestamp_timeout: int = Field(default=50, ge=1, le=10000)  # ms
    hybrid_e2e: bool = False
    inhibit_multicast_service: bool = False

    # — QoS DiffServ Code Point (AES67-recommended: 46 = EF) —
    dscp_event: int = Field(default=46, ge=0, le=63)
    dscp_general: int = Field(default=46, ge=0, le=63)

    # — Servo —
    clock_servo: Literal["pi", "linreg", "nullf", "refclock_sock"] = "pi"
    step_threshold: float = Field(default=0.000002, ge=0.0, le=10.0)  # seconds
    first_step_threshold: float = Field(default=0.000020, ge=0.0, le=10.0)
    max_frequency: int = Field(default=900_000_000, ge=1, le=1_000_000_000)  # ppb

    # — Companion phc2sys daemon —
    phc2sys_enabled: bool = True


class Aes67Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    multicast_group: str = "239.69.10.10"
    port: int = Field(default=5004, ge=1024, le=65535)
    loop: bool = True
    channels: int = Field(default=2, ge=1, le=8)
    sample_rate: int = 48000
    audio_format: Literal["S16BE", "S24BE", "S32BE"] = "S16BE"
    # 4 ms ptime cuts the sender's per-packet CPU ~4x vs the AES67-default
    # 1 ms while staying under any human-perceptible latency. On a Pi 3
    # software-timed RTP at 1 ms saturates one core just to wake up 1000
    # times/second to emit a 192-byte packet — 4 ms is the right default
    # for our hardware. Users who really need 1 ms can lower it.
    ptime_ms: float = Field(default=4.0, ge=0.125, le=10.0)
    # Receiver-side jitter buffer in ms. 20 ms is tight and prone to crackles
    # on a busy or jittery network (WiFi, multi-hop). 50 ms is a safe default
    # for a wired LAN with PTP. Bump higher (100-200 ms) if you hear glitches.
    recv_buffer_ms: int = Field(default=50, ge=5, le=500)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sap: SapSettings = SapSettings()
    ptp: PtpSettings = PtpSettings()
    aes67: Aes67Defaults = Aes67Defaults()


_settings: Settings = Settings()
_path: Path | None = None


def get() -> Settings:
    """Return the live settings (singleton)."""
    return _settings


def init(data_dir: Path) -> None:
    """Load settings from data_dir/settings.json if present, else defaults."""
    global _settings, _path
    _path = data_dir / "settings.json"
    if _path.exists():
        try:
            _settings = Settings.model_validate_json(_path.read_text())
            logger.info("settings.loaded", path=str(_path))
        except Exception:
            logger.warning("settings.load_failed", path=str(_path), exc_info=True)


def _save() -> None:
    if _path is None:
        return
    _path.parent.mkdir(parents=True, exist_ok=True)
    _path.write_text(_settings.model_dump_json(indent=2))
    with contextlib.suppress(OSError):
        _path.chmod(0o600)
    logger.info("settings.saved", path=str(_path))


@router.get("", response_model=Settings)
async def get_settings() -> Settings:
    return _settings


class SettingsPatch(BaseModel):
    """Partial update — any subset of sections."""

    model_config = ConfigDict(extra="forbid")
    sap: SapSettings | None = None
    ptp: PtpSettings | None = None
    aes67: Aes67Defaults | None = None


@router.patch("", response_model=Settings)
async def patch_settings(patch: SettingsPatch, request: Request) -> Settings:
    global _settings
    data = _settings.model_dump()
    if patch.sap is not None:
        data["sap"] = patch.sap.model_dump()
    if patch.ptp is not None:
        data["ptp"] = patch.ptp.model_dump()
    if patch.aes67 is not None:
        data["aes67"] = patch.aes67.model_dump()
    _settings = Settings.model_validate(data)
    _save()
    # Notify subscribers (e.g. SAP loop period change)
    try:
        from phonon_stage.api.aes67 import settings_changed

        await settings_changed()
    except Exception:
        pass
    # Reconcile PTP services with the new ptp.enabled flag
    if patch.ptp is not None:
        try:
            from phonon_stage.api.ptp import apply_settings as ptp_apply

            await ptp_apply()
        except Exception:
            logger.warning("settings.ptp_apply_failed", exc_info=True)
    return _settings


@router.get("/network-interfaces")
async def list_interfaces() -> list[dict[str, Any]]:
    """List network interfaces available for PTP/AES67 binding."""
    out: list[dict[str, Any]] = []
    try:
        for ifname in os.listdir("/sys/class/net"):
            if ifname == "lo":
                continue
            ip = ""
            try:
                # Best-effort IPv4 lookup
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(0.5)
                    import fcntl
                    import struct as _s

                    SIOCGIFADDR = 0x8915  # noqa: N806 — kernel ioctl constant, keep upper-case
                    packed = _s.pack("256s", ifname[:15].encode())
                    raw = fcntl.ioctl(s.fileno(), SIOCGIFADDR, packed)
                    ip = socket.inet_ntoa(raw[20:24])
            except OSError:
                pass
            kind = "wireless" if Path(f"/sys/class/net/{ifname}/wireless").exists() else "wired"
            out.append({"name": ifname, "ip": ip, "kind": kind})
    except OSError:
        pass
    return out
