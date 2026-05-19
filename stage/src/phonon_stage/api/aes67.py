"""AES67 RTP send/recv management.

Each AES67 stream is hosted by its own dedicated `pipewire -c <conf>`
subprocess — the same pattern PipeWire uses for filter-chain.service.
The subprocess loads a single module-rtp-sink (send) or module-rtp-source
(recv) and exposes a node to the main daemon over the PW socket.

Why one process per stream:
  • `pw-cli load-module` doesn't persist (loads in client process only)
  • Restarting the whole user session (pipewire+wireplumber+pipewire-pulse)
    on every create/delete was wiping the SSRC of every other active send,
    breaking already-subscribed recvs on neighbouring Stages until they
    were torn down and rebuilt.

With per-stream processes: create = spawn, delete = SIGTERM. Other
streams keep running undisturbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
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
# Legacy location — confs here are auto-loaded by the main PW daemon at
# startup. We migrate any leftovers OUT of here at boot so they don't
# double-load on top of our own subprocess.
_LEGACY_CONF_DIR = _HOME / ".config/pipewire/pipewire.conf.d"
# New home — outside PW's auto-load paths. Each conf is loaded by its own
# dedicated `pipewire -c <path>` subprocess instead.
_CONF_DIR = _HOME / "aes67-streams"
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
    ptime_ms: float = Field(default=4.0, ge=0.125, le=10.0)
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
    # IP_MULTICAST_LOOP — true means packets sent on this socket also
    # come back to local subscribers (useful when send + recv coexist
    # on the same host for testing). Surfaced to the UI so the user
    # can tell at a glance whether a stream is local-loopback enabled.
    loop: bool = True


def _node_name(kind: str, name: str) -> str:
    return f"aes67-{kind}-{name}"


def _conf_path(stream_id: str) -> Path:
    return _CONF_DIR / f"{_CONF_PREFIX}{stream_id}.conf"


# ── Per-stream PipeWire subprocess lifecycle ─────────────────────────
# Each AES67 stream gets its own `pipewire -c <conf>` child process.
# This isolates SSRC churn: deleting/recreating one stream no longer
# kills the RTP sessions of all the others (which is what the
# whole-daemon restart was doing).


async def _spawn_stream_process(stream_id: str, conf_path: Path) -> int:
    """Spawn a dedicated `pipewire -c <conf>` for one AES67 stream.

    Returns the PID. The child connects to the main user-session PW
    daemon over its socket and exposes the rtp-sink / rtp-source module
    it hosts as a regular node in the daemon's graph. `start_new_session`
    detaches the child from phonon-stage's process group so a daemon
    crash doesn't take audio with it (the child still gets reaped by
    init eventually, but stays alive long enough for the next
    phonon-stage to find and reattach to it)."""
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/pipewire",
        "-c",
        str(conf_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = proc.pid
    # Don't await proc; we want it running. Cache the Process so we can
    # wait on it later if needed (otherwise asyncio whines about
    # unawaited subprocess transports).
    _stream_procs[stream_id] = proc
    logger.info("aes67.stream_process_spawned", stream_id=stream_id, pid=pid, conf=str(conf_path))
    return pid


def _kill_stream_process(stream_id: str, pid: int) -> None:
    """SIGTERM the dedicated pipewire process for one stream.

    Best-effort: missing PID, dead process, and permission errors are
    all silently swallowed — we still drop the in-memory state."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGTERM)
    _stream_procs.pop(stream_id, None)
    logger.info("aes67.stream_process_killed", stream_id=stream_id, pid=pid)


def _process_alive(pid: int) -> bool:
    """Check if a PID is alive without raising. signal 0 = probe only."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# Active child processes keyed by stream_id. Holds the asyncio Process
# objects so they don't get garbage-collected mid-life (which would
# orphan the subprocess transport).
_stream_procs: dict[str, asyncio.subprocess.Process] = {}


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
      sess.ts-refclk = "clock.system"
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
    # sess.ts-direct = true would use the RTP timestamps verbatim, which
    # only works when sender and receiver share a synchronised PTP clock.
    # We don't run PTP grandmaster on the lab yet, so the system clocks
    # drift apart and the receiver sees timestamps "millions of samples
    # in the past" → permanent underrun, audio stays silent even though
    # packets arrive. With ts-direct=false the RTP-source module uses
    # `sess.latency.msec` as a jitter buffer and resamples around clock
    # skew — works on any unsynchronised pair of machines.
    return f"""context.modules = [
  {{ name = libpipewire-module-rtp-source
    args = {{
      source.ip = {req.multicast_group}
      source.port = {req.port}
      sess.latency.msec = {latency_ms}
      sess.name = "{node_name}"
      sess.ts-refclk = "clock.system"
      sess.ts-direct = false
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


async def sync_bt_state(reason: str) -> dict[str, int]:
    """Lighter post-action cousin of replay_audio_state, scoped to BT.

    Use cases:
      * bluealsa restart — the bridge wrapper shells self-heal (their
        `while true` loop reconnects arecord), but the new pacat that
        takes over registers a fresh PW node ID, which orphans any
        link the user had built against the previous one.
      * bluetooth restart — controllers reset, devices momentarily
        disconnect; the next sync rebuilds bridges for whoever's
        still connected.
      * BT connect via UI — bluealsa exposes a new PCM ~2s after
        BlueZ confirms the connect; we want to bridge it without
        forcing the user to click 'Sync Inputs/Outputs'.

    Doesn't tear down existing bridges (cheaper than replay_audio_state).
    Runs sync_bridges_impl to discover new/dropped PCMs, then resyncs
    mappings with retry so links re-attach to current node IDs.
    """
    summary: dict[str, int] = {"bridges_active": 0, "mappings_total": 0, "mappings_ok": 0}

    from phonon_stage.api.bluealsa_bridge import sync_bridges_impl

    sync_result = await sync_bridges_impl(buffer_ms=50)
    summary["bridges_active"] = int(sync_result.get("active", 0) or 0)

    if _mapping_service is not None:
        try:
            resync = getattr(_mapping_service, "resync_mappings", None)
            if resync is not None:
                from phonon_stage.pipewire import cli as _cli

                final: dict[str, int] = {"total": 0, "ok": 0, "skipped": 0}
                for attempt in range(3):
                    await asyncio.sleep(1.0 if attempt == 0 else 0.7)
                    _cli._pw_dump_cache = []
                    _cli._pw_dump_cache_time = 0.0
                    final = await resync()
                    if final.get("skipped", 0) == 0:
                        break
                summary["mappings_total"] = int(final.get("total", 0))
                summary["mappings_ok"] = int(final.get("ok", 0))
                logger.info("audio_replay.bt_mappings", reason=reason, **final)
        except Exception:
            logger.warning("audio_replay.bt_mappings_failed", exc_info=True)
    return summary


# Replay is sequential — two parallel replay paths (the manual UI
# button and the _system_loop's auto-detect of fresh PIDs) would race
# on _active_bridges, on the mixer's _owned_* tracking, and on the
# pw_dump cache. See audit BUG #9.
_replay_lock = asyncio.Lock()


async def replay_audio_state(reason: str = "manual") -> dict[str, int]:
    """Re-apply our app-level audio state after PipeWire/WirePlumber/
    pipewire-pulse have just been restarted.

    Restarting the audio stack — whether via the UI's 'restart audio
    stack' button, a per-service restart on pipewire/*, or an outside
    `systemctl --user restart` — wipes every bluealsa null-sink, every
    user-created link, and the AES67 modules. systemd reloads the
    .conf snippets in /etc/pipewire/pipewire.conf.d/ but doesn't know
    about our runtime state. This routine rebuilds it.

    Steps (in order):
      1. Kill stale bridge shells (tracked + sweep orphans) so the
         next start doesn't fight a zombie pacat.
      2. Recreate every bluealsa bridge from the snapshot we held.
      3. Re-attach persisted user mappings to the freshly-numbered
         PipeWire nodes (3 retries — pacat sinks take time to appear).

    Returns a small summary dict for logging/UI feedback. Calls
    concurrent with a replay-in-flight are coalesced — the second
    caller gets the same summary the first run produced.
    """
    if _replay_lock.locked():
        logger.info("audio_replay.concurrent_call_queued", reason=reason)
    async with _replay_lock:
        return await _replay_audio_state_impl(reason)


async def _replay_audio_state_impl(reason: str) -> dict[str, int]:
    summary: dict[str, int] = {"bridges": 0, "mappings_total": 0, "mappings_ok": 0}

    # 1+2 — bluealsa bridges
    try:
        import contextlib
        import signal

        from phonon_stage.api.bluealsa_bridge import _active_bridges, create_bridge

        snapshot = list(_active_bridges.items())
        _active_bridges.clear()

        for _, bridge in snapshot:
            pid = bridge.get("bridge_pid")
            if pid is not None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.kill(int(str(pid)), signal.SIGTERM)

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
        summary["bridges"] = len(snapshot)
        if snapshot:
            logger.info("audio_replay.bluealsa_bridges", reason=reason, count=len(snapshot))
    except Exception:
        logger.warning("audio_replay.bluealsa_failed", exc_info=True)

    # 3 — mappings (PipeWire links)
    if _mapping_service is not None:
        try:
            resync = getattr(_mapping_service, "resync_mappings", None)
            if resync is not None:
                from phonon_stage.pipewire import cli as _cli

                final: dict[str, int] = {"total": 0, "ok": 0, "skipped": 0}
                for attempt in range(3):
                    await asyncio.sleep(1.5 if attempt == 0 else 1.0)
                    _cli._pw_dump_cache = []
                    _cli._pw_dump_cache_time = 0.0
                    final = await resync()
                    if final.get("skipped", 0) == 0:
                        break
                summary["mappings_total"] = int(final.get("total", 0))
                summary["mappings_ok"] = int(final.get("ok", 0))
                logger.info("audio_replay.mappings", reason=reason, **final)
        except Exception:
            logger.warning("audio_replay.mappings_failed", exc_info=True)

    # 4 — source-plugin null-sinks (airplay_in, spotify_in, etc).
    # Must run BEFORE the mixer reconcile so the mixer finds the
    # source nodes when re-linking sources → master. Symptom that
    # made us add this step: after audio-stack/restart the mixer's
    # master + loopbacks came back but airplay_in / spotify_in
    # stayed gone — operator saw a working master with no sources
    # feeding it until they hit /plugins/admin/heal-null-sinks.
    try:
        from phonon_stage.main import app as _app

        reg = getattr(_app.state, "plugin_registry", None)
        if reg is not None:
            healed = await reg.heal_null_sinks()
            logger.info("audio_replay.plugin_null_sinks_healed", reason=reason, **healed)
            summary["plugin_null_sinks_healed"] = sum(
                1 for v in healed.values() if v == "ok"
            )
    except Exception:
        logger.warning("audio_replay.plugin_null_sinks_failed", exc_info=True)
        summary["plugin_null_sinks_healed"] = 0

    # 5 — mixer (master null-sink + per-output filter-chains/loopbacks).
    # Without this the mixer's persisted state stays in memory but its
    # live PW resources are gone: phonon_master null-sink, filter-chain
    # confs and module-loopback entries are all reset by the PA shim
    # restart. Symptom we caught: VU on the filter-chain output kept
    # working (filter-chain conf reloaded on its own), but the loopback
    # output (insert-less) was silent until a manual /mixer/admin/reconcile.
    #
    # We call full_resync() not _reconcile() because the pipewire-pulse
    # restart wiped the modules pactl-side; _owned_loopbacks /
    # _owned_links / _owned_chains in memory now reference dead module
    # ids. Clearing them first prevents the next reconcile from trying
    # to unload phantom modules (no harm, just noise) AND prevents
    # _apply_filter_chain_diff from seeing a stale "wanted == owned"
    # match and skipping the diff that would actually rewrite the confs
    # — see audit BUG #3.
    try:
        from phonon_stage.main import app as _app

        svc = getattr(_app.state, "mixer_service", None)
        if svc is not None:
            svc._owned_loopbacks.clear()
            svc._owned_links.clear()
            svc._owned_chains.clear()
            await svc.full_resync()
            logger.info("audio_replay.mixer_reconciled", reason=reason)
            summary["mixer_reconciled"] = 1
    except Exception:
        logger.warning("audio_replay.mixer_failed", exc_info=True)
        summary["mixer_reconciled"] = 0
    return summary


def _migrate_legacy_confs() -> None:
    """One-shot migration: lift any leftover phonon-aes67-*.conf out of
    `~/.config/pipewire/pipewire.conf.d/` into `~/aes67-streams/`.

    Before per-stream subprocess management the conf files lived under
    pipewire.conf.d/ so the main daemon would auto-load them at startup.
    Now we spawn our own pipewire children, so confs in the legacy path
    would double-load on top of our subprocess. Move them on first boot
    after upgrade; subsequent boots see an empty legacy dir and skip."""
    if not _LEGACY_CONF_DIR.is_dir():
        return
    legacy_confs = list(_LEGACY_CONF_DIR.glob(f"{_CONF_PREFIX}*.conf"))
    if not legacy_confs:
        return
    _CONF_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in legacy_confs:
        dst = _CONF_DIR / src.name
        try:
            src.rename(dst)
            moved += 1
        except OSError as e:
            logger.warning("aes67.migrate_legacy_failed", src=str(src), error=str(e))
    if moved:
        logger.info("aes67.migrated_legacy_confs", count=moved, dest=str(_CONF_DIR))


async def restore_existing_aes67() -> None:
    """Re-populate _active_streams from the conf files on disk and spawn
    a `pipewire -c <conf>` subprocess for each one.

    Called at daemon startup. Migrates legacy confs out of
    pipewire.conf.d/ first, then re-spawns processes for every surviving
    snippet so AES67 streams come back transparently after a phonon-stage
    restart."""
    _migrate_legacy_confs()
    if not _CONF_DIR.is_dir():
        return
    for path in sorted(_CONF_DIR.glob(f"{_CONF_PREFIX}*.conf")):
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
        loop_match = re.search(r"net\.loop\s*=\s*(true|false)", text)
        try:
            pid = await _spawn_stream_process(stream_id, path)
        except OSError as e:
            logger.warning("aes67.restore_spawn_failed", stream_id=stream_id, error=str(e))
            continue
        _active_streams[stream_id] = {
            "kind": kind,
            "name": name_match.group(1) if name_match else stream_id,
            "multicast_group": ip_match.group(1) if ip_match else "",
            "port": int(port_match.group(1)) if port_match else 0,
            "channels": int(ch_match.group(1)) if ch_match else 2,
            "sample_rate": int(rate_match.group(1)) if rate_match else 48000,
            "audio_format": fmt_match.group(1) if fmt_match else "S16BE",
            "loop": loop_match.group(1) == "true" if loop_match else True,
            "conf_path": str(path),
            "pid": pid,
        }
    if _active_streams:
        logger.info("aes67.streams_restored", count=len(_active_streams))
        # Push the recomputed mode (STANDALONE → MESH) into mDNS TXT.
        # Without this, the daemon comes up advertising STANDALONE
        # forever after a restart, even though it has live AES67
        # streams — neighbouring stages then see this Stage as
        # 'STANDALONE' and refuse to subscribe.
        await _announce_mode()


async def cleanup_stale_aes67() -> None:
    """Wipe ALL phonon-aes67-* config files and kill their PW subprocesses.

    Destructive — only kept for the explicit "reset to factory" flow.
    NOT called at startup anymore."""
    if not _CONF_DIR.is_dir():
        _active_streams.clear()
        return
    # Kill running subprocesses first so they don't keep stale confs alive
    for sid, info in list(_active_streams.items()):
        pid = info.get("pid")
        if isinstance(pid, int):
            _kill_stream_process(sid, pid)
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

    # Spawn the dedicated pipewire process BEFORE registering the stream
    # in _active_streams — if the spawn fails the conf file is left on
    # disk but we don't bookkeep a stream that has no audio behind it.
    pid = await _spawn_stream_process(stream_id, path)

    _active_streams[stream_id] = {
        "kind": kind,
        "name": req.name,
        "multicast_group": req.multicast_group,
        "port": req.port,
        "channels": req.channels,
        "sample_rate": req.sample_rate,
        "audio_format": req.audio_format,
        "loop": req.loop,
        "conf_path": str(path),
        "pid": pid,
    }

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
        loop=req.loop,
    )


@router.delete("/{stream_id}")
async def delete_stream(stream_id: str) -> dict[str, str]:
    """Remove an AES67 stream — kill its dedicated PW subprocess and
    drop its conf. Doesn't touch any other stream's process."""
    info = _active_streams.pop(stream_id, None)
    if info is None:
        raise HTTPException(status_code=404, detail=f"stream {stream_id} not found")

    pid = info.get("pid")
    if isinstance(pid, int):
        _kill_stream_process(stream_id, pid)

    path = Path(str(info.get("conf_path", "")))
    if path.exists():
        path.unlink()
    logger.info("aes67.stream_deleted", stream_id=stream_id)

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
            loop=bool(s.get("loop", True)),
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
    is_local: bool = False  # True when source_ip == our own LAN IP — multicast
    # loopback makes us see our own SAP announcements; the UI hides these from
    # the subscribe list since you cannot subscribe to your own send.


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
    """List AES67 streams currently being announced via SAP.

    Streams whose source IP matches our own LAN IP are tagged
    `is_local=true` — the SAP listener sees our own announcements
    via multicast loopback. They're returned so callers can show
    them as "my own send" if useful, but the standalone UI filters
    them out of the subscribe list.
    """
    now = time.monotonic()
    own_ip = _own_lan_ip(_SAP_GROUP)
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
            is_local=str(v.get("source_ip", "")) == own_ip,
        )
        for k, v in _discovered_streams.items()
    ]


class SubscribeRequest(BaseModel):
    """Subscribe to a discovered stream — auto-creates a recv with the stream's params."""

    key: str
    name: str = Field(min_length=1, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    # Default to False for subscribed recv streams — IP_MULTICAST_LOOP
    # only affects packets we *send*, so it's a no-op on a pure receiver
    # socket. Leaving it true just lit the ↻ icon in the UI for no
    # actual loopback behaviour.
    loop: bool = False


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
