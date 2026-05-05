"""Fake Phonon Stage simulator — mDNS + minimal HTTP API + SAP announce.

Runs alongside the gstreamer echo pipeline. Makes the container visible
on the network as if it were a second Stage:

  * Announces _phonon-stage._tcp.local via mDNS-SD with a chosen stage_id
  * Serves /health and /capabilities so a real Stage that fetches
    these doesn't get 404
  * Periodically announces the outbound RTP stream via SAP/SDP so any
    SAP listener (other Stage, network analyzer, etc.) sees it
"""

from __future__ import annotations

import asyncio
import os
import random
import socket
import struct
from typing import Any

from aiohttp import web
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

STAGE_ID = os.environ.get("FAKE_STAGE_ID", "stage-echo01")
HTTP_PORT = int(os.environ.get("FAKE_HTTP_PORT", "8402"))
OUT_GROUP = os.environ.get("OUT_GROUP", "239.69.10.20")
OUT_PORT = int(os.environ.get("OUT_PORT", "5004"))
IN_GROUP = os.environ.get("IN_GROUP", "239.69.10.10")
IN_PORT = int(os.environ.get("IN_PORT", "5004"))

SAP_GROUP = "239.255.255.255"
SAP_PORT = 9875


def _own_ip() -> str:
    """Best-effort: return the host IP we'd use to reach the multicast network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((OUT_GROUP, 1))
        return str(s.getsockname()[0])
    finally:
        s.close()


# ── HTTP API ─────────────────────────────────────────────────────

async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "uptime_seconds": 0,  # we don't track it, doesn't matter
        "stage_id": STAGE_ID,
    })


async def capabilities(_request: web.Request) -> web.Response:
    return web.json_response({
        "stage_id": STAGE_ID,
        "mode": "STANDALONE",
        "audio_devices": [],
        "bluetooth_controllers": [],
        "aes67_streams": [
            {
                "name": "echo-out",
                "kind": "send",
                "multicast_group": OUT_GROUP,
                "port": OUT_PORT,
                "channels": 2,
                "sample_rate": 48000,
                "audio_format": "S16BE",
            },
            {
                "name": "echo-in",
                "kind": "recv",
                "multicast_group": IN_GROUP,
                "port": IN_PORT,
                "channels": 2,
                "sample_rate": 48000,
                "audio_format": "S16BE",
            },
        ],
    })


# ── SAP announcer ────────────────────────────────────────────────

def _build_sdp(stream_name: str, src_ip: str, mcast: str, port: int) -> str:
    session_id = random.randint(1, 2**31)
    return (
        f"v=0\r\n"
        f"o=- {session_id} 1 IN IP4 {src_ip}\r\n"
        f"s={stream_name}\r\n"
        f"c=IN IP4 {mcast}/32\r\n"
        f"t=0 0\r\n"
        f"a=recvonly\r\n"
        f"a=tool:phonon-aes67-echo\r\n"
        f"m=audio {port} RTP/AVP 96\r\n"
        f"a=rtpmap:96 L16/48000/2\r\n"
        f"a=ptime:1\r\n"
        f"a=mediaclk:direct=0\r\n"
    )


def _build_sap_packet(sdp: str, src_ip: str) -> bytes:
    # SAP header (RFC 2974): V=1, A=0 (IPv4), R=0, T=0 (announce), E=0, C=0
    flags = 0x20
    auth_len = 0
    msg_id_hash = random.randint(0, 0xFFFF)
    src_addr = socket.inet_aton(src_ip)
    header = struct.pack("!BBH", flags, auth_len, msg_id_hash) + src_addr
    payload_type = b"application/sdp\x00"
    return header + payload_type + sdp.encode("utf-8")


async def sap_announcer() -> None:
    src_ip = _own_ip()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    try:
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(src_ip),
        )
    except OSError:
        pass
    print(f"[sap] announcing from {src_ip} for {OUT_GROUP}:{OUT_PORT}", flush=True)
    while True:
        sdp = _build_sdp(f"{STAGE_ID}-out", src_ip, OUT_GROUP, OUT_PORT)
        pkt = _build_sap_packet(sdp, src_ip)
        try:
            sock.sendto(pkt, (SAP_GROUP, SAP_PORT))
        except OSError as e:
            print(f"[sap] send failed: {e}", flush=True)
        await asyncio.sleep(10)


# ── mDNS register ────────────────────────────────────────────────

async def mdns_register(azc: AsyncZeroconf) -> AsyncServiceInfo:
    src_ip = _own_ip()
    info = AsyncServiceInfo(
        type_="_phonon-stage._tcp.local.",
        name=f"{STAGE_ID}._phonon-stage._tcp.local.",
        addresses=[socket.inet_aton(src_ip)],
        port=HTTP_PORT,
        properties={
            "stage_id": STAGE_ID,
            "version": "0.1.0-echo",
            "mode": "STANDALONE",
        },
    )
    await azc.async_register_service(info)
    print(f"[mdns] registered {STAGE_ID} at {src_ip}:{HTTP_PORT}", flush=True)
    return info


# ── Main ─────────────────────────────────────────────────────────

async def main() -> None:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/capabilities", capabilities)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)  # noqa: S104
    await site.start()
    print(f"[http] serving on :{HTTP_PORT}", flush=True)

    azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    await mdns_register(azc)

    sap_task = asyncio.create_task(sap_announcer())

    try:
        # Sleep forever; rely on container stop signal to exit
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        sap_task.cancel()
        await azc.async_close()
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
