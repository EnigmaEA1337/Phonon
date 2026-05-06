"""AES67 RTP send/recv management.

Generates PipeWire config snippets in `~/.config/pipewire/pipewire.conf.d/`
to load module-rtp-sink (send) and module-rtp-source (recv) instances.
Each AES67 stream becomes a regular PipeWire node — patchable in the UI
just like any other source/sink.

PipeWire restart is required after each create/delete because the
runtime `pw-cli load-module` path is fragile. The restart is short
(~3 s) but disrupts other audio briefly. Acceptable for dev/test;
worth replacing with native protocol load-module later.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import struct
import time
import uuid
from pathlib import Path
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/aes67", tags=["aes67"])

logger = structlog.get_logger()

_HOME = Path(os.environ.get("HOME", "/home/phonon"))
_CONF_DIR = _HOME / ".config/pipewire/pipewire.conf.d"
_CONF_PREFIX = "phonon-aes67-"

# Track active streams: id -> { kind, name, group, port, channels, conf_path }
_active_streams: dict[str, dict[str, object]] = {}

# Track discovered SAP streams: hash_key -> { source_ip, name, group, port, ..., last_seen }
_discovered_streams: dict[str, dict[str, object]] = {}
_DISCOVERED_TTL = 30.0  # seconds — drop entries not refreshed in this window
_SAP_GROUP = "239.255.255.255"
_SAP_PORT = 9875

# Optional reference to the discovery backend so we can update mDNS TXT
# records when stream count changes (STANDALONE ↔ MESH).
_discovery_backend: object | None = None

# Optional reference to the mapping service so a PW restart can rebuild
# every persisted user mapping after bridges are back up.
_mapping_service: object | None = None


def set_discovery_backend(backend: object) -> None:
    """Wire up the discovery backend so we can announce mode changes."""
    global _discovery_backend
    _discovery_backend = backend


def set_mapping_service(service: object) -> None:
    """Wire up the mapping service so we can auto-resync after PW restart."""
    global _mapping_service
    _mapping_service = service


def current_mode() -> str:
    """Compute the Stage's current network mode from active AES67 streams."""
    return "MESH" if _active_streams else "STANDALONE"


async def _announce_mode() -> None:
    """Push the current mode to mDNS TXT if a discovery backend is wired up."""
    if _discovery_backend is None:
        return
    update_mode = getattr(_discovery_backend, "update_mode", None)
    if update_mode is None:
        return
    try:
        await update_mode(current_mode())
    except Exception:
        logger.warning("aes67.mode_announce_failed", exc_info=True)


class CreateStreamRequest(BaseModel):
    """Request to create an AES67 send or recv stream."""

    name: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    multicast_group: str = Field(default="239.69.10.10")
    port: int = Field(default=5004, ge=1024, le=65535)
    channels: int = Field(default=2, ge=1, le=8)
    sample_rate: int = Field(default=48000)
    audio_format: str = Field(default="S16BE")
    ptime_ms: float = Field(default=1.0, ge=0.125, le=10.0)
    loop: bool = Field(default=True, description="IP_MULTICAST_LOOP — true for local-host testing")
    # Receiver-only — ignored for send streams.
    recv_buffer_ms: int | None = Field(
        default=None,
        ge=5,
        le=500,
        description="recv jitter buffer (ms). Falls back to Settings.aes67.recv_buffer_ms.",
    )


class StreamInfo(BaseModel):
    """Info about an active AES67 stream."""

    id: str
    kind: Literal["send", "recv"]
    name: str
    multicast_group: str
    port: int
    channels: int
    sample_rate: int
    audio_format: str


def _node_name(kind: str, name: str) -> str:
    return f"aes67-{kind}-{name}"


def _conf_path(stream_id: str) -> Path:
    return _CONF_DIR / f"{_CONF_PREFIX}{stream_id}.conf"


def _render_send_conf(req: CreateStreamRequest, node_name: str) -> str:
    return f"""context.modules = [
  {{ name = libpipewire-module-rtp-sink
    args = {{
      destination.ip = {req.multicast_group}
      destination.port = {req.port}
      net.ttl = 1
      net.loop = {"true" if req.loop else "false"}
      sess.name = "{node_name}"
      sess.min-ptime = {req.ptime_ms}
      sess.max-ptime = {req.ptime_ms}
      audio.format = {req.audio_format}
      audio.rate = {req.sample_rate}
      audio.channels = {req.channels}
      stream.props = {{
        node.name = "{node_name}"
        node.description = "AES67 Send {req.name}"
        media.class = Audio/Sink
      }}
    }}
  }}
]
"""


def _render_recv_conf(req: CreateStreamRequest, node_name: str) -> str:
    # Per-stream override > Settings default. Settings default is the
    # tunable jitter buffer the user adjusts when they hear crackles.
    from phonon_stage.api import settings as _settings_mod

    if req.recv_buffer_ms is not None:
        latency_ms = req.recv_buffer_ms
    else:
        latency_ms = _settings_mod.get().aes67.recv_buffer_ms
    return f"""context.modules = [
  {{ name = libpipewire-module-rtp-source
    args = {{
      source.ip = {req.multicast_group}
      source.port = {req.port}
      sess.latency.msec = {latency_ms}
      sess.name = "{node_name}"
      audio.format = {req.audio_format}
      audio.rate = {req.sample_rate}
      audio.channels = {req.channels}
      stream.props = {{
        node.name = "{node_name}"
        node.description = "AES67 Recv {req.name}"
        media.class = Audio/Source
      }}
    }}
  }}
]
"""


_sap_announcer_task: asyncio.Task[None] | None = None


def _own_lan_ip(target_mcast: str = "239.255.255.255") -> str:
    """Best-effort: pick the LAN IP we'd use to send to multicast."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_mcast, 1))
        return str(s.getsockname()[0])
    finally:
        s.close()


def _build_sdp(
    stream_name: str,
    src_ip: str,
    mcast: str,
    port: int,
    channels: int,
    rate: int,
    audio_format: str,
) -> str:
    import random as _r

    session_id = _r.randint(1, 2**31)
    fmt = "L16" if audio_format.upper() == "S16BE" else audio_format
    return (
        f"v=0\r\n"
        f"o=- {session_id} 1 IN IP4 {src_ip}\r\n"
        f"s={stream_name}\r\n"
        f"c=IN IP4 {mcast}/32\r\n"
        f"t=0 0\r\n"
        f"a=recvonly\r\n"
        f"a=tool:phonon-stage\r\n"
        f"m=audio {port} RTP/AVP 96\r\n"
        f"a=rtpmap:96 {fmt}/{rate}/{channels}\r\n"
        f"a=ptime:1\r\n"
        f"a=mediaclk:direct=0\r\n"
    )


def _build_sap_packet(sdp: str, src_ip: str) -> bytes:
    import random as _r

    flags = 0x20  # V=1, A=0 (IPv4), R=0, T=0 (announce), E=0, C=0
    auth_len = 0
    msg_id_hash = _r.randint(0, 0xFFFF)
    src_addr = socket.inet_aton(src_ip)
    header = struct.pack("!BBH", flags, auth_len, msg_id_hash) + src_addr
    return header + b"application/sdp\x00" + sdp.encode("utf-8")


async def _sap_announce_loop() -> None:
    """Periodically announce every active send stream via SAP/SDP."""
    from phonon_stage.api import settings as _settings_mod

    src_ip = _own_lan_ip(_SAP_GROUP)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(src_ip))
    logger.info("aes67.sap_announcer_started", src_ip=src_ip)
    while True:
        cfg = _settings_mod.get().sap
        try:
            if cfg.announce_enabled:
                for sid, s in list(_active_streams.items()):
                    if s.get("kind") != "send":
                        continue
                    stream_name = f"phonon-{sid[:6]}-{s.get('name', '')}"
                    sdp = _build_sdp(
                        stream_name,
                        src_ip,
                        str(s.get("multicast_group", "")),
                        int(s.get("port", 5004)),  # type: ignore[call-overload]
                        int(s.get("channels", 2)),  # type: ignore[call-overload]
                        int(s.get("sample_rate", 48000)),  # type: ignore[call-overload]
                        str(s.get("audio_format", "S16BE")),
                    )
                    pkt = _build_sap_packet(sdp, src_ip)
                    with contextlib.suppress(OSError):
                        sock.sendto(pkt, (_SAP_GROUP, _SAP_PORT))
        except Exception:
            logger.warning("aes67.sap_announce_error", exc_info=True)
        await asyncio.sleep(max(2, int(cfg.announce_interval_s)))


async def start_sap_announcer() -> None:
    """Start the background SAP announcer if not already running."""
    global _sap_announcer_task
    if _sap_announcer_task is not None and not _sap_announcer_task.done():
        return
    _sap_announcer_task = asyncio.create_task(_sap_announce_loop())


async def settings_changed() -> None:
    """Hook called by /settings PATCH so we react to runtime config changes."""
    # Currently a no-op — the announcer reads settings each iteration so the
    # interval and enabled flag take effect within the next ≤2s tick. If we
    # add SAP listen toggle, that's where we'd start/stop the listener.
    return


async def _restart_pipewire() -> None:
    """Restart user-session PipeWire so config snippets are reloaded.

    Restarting wipes pactl null-sinks (bluealsa bridges) and any
    user-created PipeWire links. We trigger a bluealsa resync after
    PW comes back so BT bridges are recreated automatically. Mappings
    aren't auto-restored — the user has to recreate them.
    """
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "--user",
        "restart",
        "pipewire",
        "wireplumber",
        "pipewire-pulse",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("aes67.pipewire_restart_failed", stderr=stderr.decode())
    else:
        logger.info("aes67.pipewire_restarted")
    # Give PipeWire a moment to come back up before we touch it again
    await asyncio.sleep(2.5)

    # Auto re-create bluealsa bridges (null-sinks were wiped by PW restart).
    # IMPORTANT: kill the previous bridge shells *and* nuke any orphan
    # arecord/pacat that survived. Otherwise each PW restart leaks a
    # whole bridge-loop into background, and CPU explodes after ~10
    # AES67 operations.
    try:
        import contextlib
        import signal

        from phonon_stage.api.bluealsa_bridge import _active_bridges, create_bridge

        snapshot = list(_active_bridges.items())
        _active_bridges.clear()

        # Kill tracked bridge shells
        for _, bridge in snapshot:
            pid = bridge.get("bridge_pid")
            if pid is not None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.kill(int(str(pid)), signal.SIGTERM)

        # Belt-and-braces: kill any untracked orphans matching our bridge pattern
        sweep = await asyncio.create_subprocess_shell(
            "pkill -f 'while true.*arecord.*bluealsa' 2>/dev/null; "
            "pkill -f 'arecord.*bluealsa' 2>/dev/null; "
            "pkill -f 'pacat.*bt_' 2>/dev/null; "
            "pkill -f 'parec.*bt_' 2>/dev/null",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await sweep.communicate()
        await asyncio.sleep(0.3)

        for key, bridge in snapshot:
            parts = key.rsplit("_", 1)
            if len(parts) != 2:
                continue
            mac, dtype = parts
            await create_bridge(
                mac=mac,
                name=str(bridge.get("name", "")),
                device_type=dtype,
            )
        if snapshot:
            logger.info("aes67.bluealsa_bridges_resynced", count=len(snapshot))
    except Exception:
        logger.warning("aes67.bluealsa_resync_failed", exc_info=True)

    # And re-attach all persisted user mappings to the freshly-numbered PW
    # nodes so links survive the restart end-to-end. Without this the user
    # has to click "Resync" in the Mixer after every AES67 op.
    if _mapping_service is not None:
        try:
            resync = getattr(_mapping_service, "resync_mappings", None)
            if resync is not None:
                # bluealsa bridges spawn pacat which takes a moment to
                # register its sink-input node in PW. We invalidate the
                # pw-dump cache and retry up to 3 times so we catch it
                # without a very long static sleep.
                from phonon_stage.pipewire import cli as _cli

                final = {"total": 0, "ok": 0, "skipped": 0}
                for attempt in range(3):
                    await asyncio.sleep(1.5 if attempt == 0 else 1.0)
                    _cli._pw_dump_cache = []
                    _cli._pw_dump_cache_time = 0.0
                    final = await resync()
                    if final.get("skipped", 0) == 0:
                        break
                logger.info("aes67.mappings_resynced", **final)
        except Exception:
            logger.warning("aes67.mappings_resync_failed", exc_info=True)


async def restore_existing_aes67() -> None:
    """Re-populate _active_streams from the conf files on disk so the daemon
    knows about streams created in a previous session. PipeWire has already
    loaded these snippets at its own startup — we just need to register them
    in our in-memory map so /aes67/streams reflects reality and SAP announces
    keep firing for them.

    Replaces the previous cleanup-then-wipe strategy that erased every
    AES67 stream on every daemon restart.
    """
    if not _CONF_DIR.is_dir():
        return
    for path in _CONF_DIR.glob(f"{_CONF_PREFIX}*.conf"):
        stream_id = path.stem.removeprefix(_CONF_PREFIX)
        try:
            text = path.read_text()
        except OSError:
            continue
        # Heuristic parse — kind from module name, fields from regex
        kind: str = "send" if "module-rtp-sink" in text else "recv"
        ip_match = re.search(r"(?:destination|source)\.ip\s*=\s*([\d.]+)", text)
        port_match = re.search(r"(?:destination|source)\.port\s*=\s*(\d+)", text)
        ch_match = re.search(r"audio\.channels\s*=\s*(\d+)", text)
        rate_match = re.search(r"audio\.rate\s*=\s*(\d+)", text)
        fmt_match = re.search(r"audio\.format\s*=\s*(\S+)", text)
        name_match = re.search(r'node\.name\s*=\s*"aes67-\w+-([^"]+)"', text)
        _active_streams[stream_id] = {
            "kind": kind,
            "name": name_match.group(1) if name_match else stream_id,
            "multicast_group": ip_match.group(1) if ip_match else "",
            "port": int(port_match.group(1)) if port_match else 0,
            "channels": int(ch_match.group(1)) if ch_match else 2,
            "sample_rate": int(rate_match.group(1)) if rate_match else 48000,
            "audio_format": fmt_match.group(1) if fmt_match else "S16BE",
            "conf_path": str(path),
        }
    if _active_streams:
        logger.info("aes67.streams_restored", count=len(_active_streams))


async def cleanup_stale_aes67() -> None:
    """Wipe ALL phonon-aes67-* config files. Destructive — only kept for
    explicit "reset to factory" flow. NOT called at startup anymore."""
    if not _CONF_DIR.is_dir():
        return
    removed = 0
    for path in _CONF_DIR.glob(f"{_CONF_PREFIX}*.conf"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    _active_streams.clear()
    if removed:
        logger.info("aes67.stale_confs_cleaned", count=removed)
        await _restart_pipewire()


@router.post("/send", status_code=201)
async def create_send(req: CreateStreamRequest) -> StreamInfo:
    """Create an AES67 sender — appears as Audio/Sink in the patch bay."""
    return await _create_stream(req, kind="send")


@router.post("/recv", status_code=201)
async def create_recv(req: CreateStreamRequest) -> StreamInfo:
    """Create an AES67 receiver — appears as Audio/Source in the patch bay."""
    return await _create_stream(req, kind="recv")


async def _create_stream(req: CreateStreamRequest, kind: str) -> StreamInfo:
    _CONF_DIR.mkdir(parents=True, exist_ok=True)

    # Reject duplicates by name
    for s in _active_streams.values():
        if s.get("kind") == kind and s.get("name") == req.name:
            raise HTTPException(
                status_code=409, detail=f"{kind} stream '{req.name}' already exists"
            )

    stream_id = uuid.uuid4().hex[:8]
    node_name = _node_name(kind, req.name)
    conf = (
        _render_send_conf(req, node_name) if kind == "send" else _render_recv_conf(req, node_name)
    )

    path = _conf_path(stream_id)
    path.write_text(conf)
    logger.info(
        "aes67.stream_conf_written",
        stream_id=stream_id,
        kind=kind,
        path=str(path),
        group=req.multicast_group,
        port=req.port,
    )

    _active_streams[stream_id] = {
        "kind": kind,
        "name": req.name,
        "multicast_group": req.multicast_group,
        "port": req.port,
        "channels": req.channels,
        "sample_rate": req.sample_rate,
        "audio_format": req.audio_format,
        "conf_path": str(path),
    }

    await _restart_pipewire()
    await _announce_mode()

    return StreamInfo(
        id=stream_id,
        kind=kind,  # type: ignore[call-overload]
        name=req.name,
        multicast_group=req.multicast_group,
        port=req.port,
        channels=req.channels,
        sample_rate=req.sample_rate,
        audio_format=req.audio_format,
    )


@router.delete("/{stream_id}")
async def delete_stream(stream_id: str) -> dict[str, str]:
    """Remove an AES67 stream and restart PipeWire."""
    info = _active_streams.pop(stream_id, None)
    if info is None:
        raise HTTPException(status_code=404, detail=f"stream {stream_id} not found")

    path = Path(str(info.get("conf_path", "")))
    if path.exists():
        path.unlink()
    logger.info("aes67.stream_deleted", stream_id=stream_id)

    await _restart_pipewire()
    await _announce_mode()
    return {"status": "deleted", "id": stream_id}


@router.get("/streams")
async def list_streams() -> list[StreamInfo]:
    """List all active AES67 streams."""
    return [
        StreamInfo(
            id=sid,
            kind=str(s.get("kind", "send")),  # type: ignore[call-overload]
            name=str(s.get("name", "")),
            multicast_group=str(s.get("multicast_group", "")),
            port=int(s.get("port", 0)),  # type: ignore[call-overload]
            channels=int(s.get("channels", 2)),  # type: ignore[call-overload]
            sample_rate=int(s.get("sample_rate", 48000)),  # type: ignore[call-overload]
            audio_format=str(s.get("audio_format", "S16BE")),
        )
        for sid, s in _active_streams.items()
    ]


# ── SAP listener ──────────────────────────────────────────────


class DiscoveredStream(BaseModel):
    """An AES67 stream announced via SAP/SDP by another node."""

    key: str
    source_ip: str
    name: str
    multicast_group: str
    port: int
    channels: int
    sample_rate: int
    audio_format: str
    last_seen_age_s: float


def _parse_sap_packet(data: bytes, sender_ip: str) -> dict[str, object] | None:
    """Parse a SAP packet (RFC 2974) and extract its SDP-described stream."""
    if len(data) < 8:
        return None
    flags = data[0]
    version = (flags >> 5) & 0x7
    addr_v6 = bool((flags >> 4) & 0x1)
    is_deletion = bool((flags >> 2) & 0x1)
    if version != 1 or addr_v6:
        return None  # only V1 IPv4 announcements supported here
    auth_len = data[1]
    msg_id_hash = (data[2] << 8) | data[3]
    addr_len = 4
    offset = 4 + addr_len + auth_len * 4
    if offset >= len(data):
        return None
    payload = data[offset:]
    # Optional payload type "application/sdp\0" prefix
    if payload.startswith(b"application/sdp\x00"):
        payload = payload[len(b"application/sdp\x00") :]
    sdp = payload.decode("utf-8", errors="ignore")
    parsed = _parse_sdp(sdp)
    if not parsed:
        return None
    parsed["msg_id_hash"] = msg_id_hash
    parsed["is_deletion"] = is_deletion
    parsed["source_ip"] = sender_ip
    return parsed


_SDP_LINE_RE = re.compile(r"^([a-z])=(.*)$")


def _parse_sdp(sdp: str) -> dict[str, object] | None:
    """Extract the fields we care about from an SDP payload."""
    name = ""
    mcast = ""
    port = 0
    channels = 2
    rate = 48000
    fmt = "L16"
    for line in sdp.splitlines():
        m = _SDP_LINE_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "s":
            name = val
        elif key == "c" and val.startswith("IN IP4 "):
            mcast = val[len("IN IP4 ") :].split("/", 1)[0]
        elif key == "m" and val.startswith("audio "):
            parts = val.split()
            if len(parts) >= 2:
                with contextlib.suppress(ValueError):
                    port = int(parts[1])
        elif key == "a" and val.startswith("rtpmap:"):
            #  rtpmap:96 L16/48000/2
            payload_def = val.split(" ", 1)[1] if " " in val else ""
            bits = payload_def.split("/")
            if len(bits) >= 2:
                fmt = bits[0]
                with contextlib.suppress(ValueError):
                    rate = int(bits[1])
            if len(bits) >= 3:
                with contextlib.suppress(ValueError):
                    channels = int(bits[2])
    if not mcast or not port:
        return None
    audio_format = "S16BE" if fmt.upper() == "L16" else fmt
    return {
        "name": name,
        "multicast_group": mcast,
        "port": port,
        "channels": channels,
        "sample_rate": rate,
        "audio_format": audio_format,
    }


class _SapListenerProtocol(asyncio.DatagramProtocol):
    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # type: ignore[override]
        parsed = _parse_sap_packet(data, addr[0])
        if not parsed:
            return
        key = f"{parsed['source_ip']}:{parsed['multicast_group']}:{parsed['port']}"
        if parsed.get("is_deletion"):
            _discovered_streams.pop(key, None)
            logger.info("aes67.sap_deletion", key=key)
            return
        prev = _discovered_streams.get(key)
        _discovered_streams[key] = {**parsed, "last_seen": time.monotonic()}
        if prev is None:
            logger.info(
                "aes67.sap_announced",
                key=key,
                name=parsed.get("name"),
                group=parsed.get("multicast_group"),
                port=parsed.get("port"),
            )


async def start_sap_listener() -> None:
    """Start a background SAP listener on 239.255.255.255:9875."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", _SAP_PORT))
    mreq = struct.pack("4sl", socket.inet_aton(_SAP_GROUP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    await loop.create_datagram_endpoint(_SapListenerProtocol, sock=sock)
    logger.info("aes67.sap_listener_started", group=_SAP_GROUP, port=_SAP_PORT)
    # Background expiry sweep — fire-and-forget, lives for the daemon's
    # lifetime; we don't need to await or cancel it here.
    asyncio.create_task(_sap_expiry_loop())  # noqa: RUF006


async def _sap_expiry_loop() -> None:
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        stale = [
            k
            for k, v in _discovered_streams.items()
            if now - float(v.get("last_seen", 0.0)) > _DISCOVERED_TTL  # type: ignore[call-overload]
        ]
        for k in stale:
            _discovered_streams.pop(k, None)
            logger.info("aes67.sap_expired", key=k)


@router.get("/discovered")
async def list_discovered() -> list[DiscoveredStream]:
    """List AES67 streams currently being announced via SAP."""
    now = time.monotonic()
    return [
        DiscoveredStream(
            key=k,
            source_ip=str(v.get("source_ip", "")),
            name=str(v.get("name", "")),
            multicast_group=str(v.get("multicast_group", "")),
            port=int(v.get("port", 0)),  # type: ignore[call-overload]
            channels=int(v.get("channels", 2)),  # type: ignore[call-overload]
            sample_rate=int(v.get("sample_rate", 48000)),  # type: ignore[call-overload]
            audio_format=str(v.get("audio_format", "S16BE")),
            last_seen_age_s=round(now - float(v.get("last_seen", now)), 1),  # type: ignore[call-overload]
        )
        for k, v in _discovered_streams.items()
    ]


class SubscribeRequest(BaseModel):
    """Subscribe to a discovered stream — auto-creates a recv with the stream's params."""

    key: str
    name: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    loop: bool = True


@router.post("/subscribe", status_code=201)
async def subscribe_to_discovered(req: SubscribeRequest) -> StreamInfo:
    """Auto-create a recv stream from a discovered SAP announcement."""
    discovered = _discovered_streams.get(req.key)
    if discovered is None:
        raise HTTPException(status_code=404, detail=f"discovered stream {req.key} not found")
    create_req = CreateStreamRequest(
        name=req.name,
        multicast_group=str(discovered.get("multicast_group", "")),
        port=int(discovered.get("port", 5004)),  # type: ignore[call-overload]
        channels=int(discovered.get("channels", 2)),  # type: ignore[call-overload]
        sample_rate=int(discovered.get("sample_rate", 48000)),  # type: ignore[call-overload]
        audio_format=str(discovered.get("audio_format", "S16BE")),
        loop=req.loop,
    )
    return await _create_stream(create_req, kind="recv")
