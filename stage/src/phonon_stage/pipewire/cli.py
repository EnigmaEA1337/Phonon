"""PipeWire subprocess wrapper — the ONLY module that calls subprocess.

All PipeWire CLI interactions (pw-cli, pw-link, wpctl) are isolated here.
No other module should import asyncio.create_subprocess_exec for PipeWire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any

import structlog

logger = structlog.get_logger()

TIMEOUT_SECONDS = 5.0


class PipeWireCliError(Exception):
    """Raised when a PipeWire CLI command fails."""

    def __init__(self, cmd: str, returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"PipeWire CLI error: {cmd} returned {returncode}: {stderr}")


async def run_command(*args: str) -> str:
    """Run a command and return stdout. Raises PipeWireCliError on failure."""
    cmd_str = " ".join(args)
    logger.debug("pipewire.cli.run", cmd=cmd_str)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise PipeWireCliError(cmd_str, -1, "timeout") from None

    stdout = stdout_bytes.decode().strip()
    stderr = stderr_bytes.decode().strip()

    if proc.returncode != 0:
        logger.warning(
            "pipewire.cli.error", cmd=cmd_str, returncode=proc.returncode, stderr=stderr
        )
        raise PipeWireCliError(cmd_str, proc.returncode or -1, stderr)

    return stdout


async def pw_dump() -> list[dict[str, Any]]:
    """Run pw-dump and return parsed JSON array of PipeWire objects.

    pw-dump may stream continuously (monitoring mode). We read with a
    short timeout — once the initial dump is sent we parse whatever we got.
    """
    proc = await asyncio.create_subprocess_exec(
        "pw-dump",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    assert proc.stdout is not None
    assert proc.stderr is not None

    chunks: list[bytes] = []

    try:
        # Read chunks until timeout (pw-dump sends the full dump quickly, then idles)
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=2.0)
            if not chunk:
                break
            chunks.append(chunk)
    except TimeoutError:
        pass  # Expected: pw-dump keeps streaming, we stop after 2s of no new data

    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()

    if not chunks:
        stderr = (await proc.stderr.read()).decode().strip()
        raise PipeWireCliError("pw-dump", proc.returncode or -1, stderr or "no output")

    full = b"".join(chunks).decode()

    # pw-dump output may be a complete JSON array, or may have trailing
    # partial data from monitoring. Try to parse as-is, then try truncating
    # at the last top-level `]`.
    try:
        return json.loads(full)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        # Find the last `]\n` which closes the top-level array
        last_bracket = full.rfind("\n]\n")
        if last_bracket == -1:
            last_bracket = full.rfind("\n]")
        if last_bracket >= 0:
            truncated = full[: last_bracket + 2]
            return json.loads(truncated)  # type: ignore[no-any-return]
        raise PipeWireCliError("pw-dump", -1, "unparseable output") from None


async def pw_link_create(output_port_id: int, input_port_id: int) -> str:
    """Create a PipeWire link. Returns raw output."""
    return await run_command("pw-link", str(output_port_id), str(input_port_id))


async def pw_link_destroy(link_id: int) -> str:
    """Destroy a PipeWire link by ID."""
    return await run_command("pw-link", "-d", str(link_id))


async def pw_link_list() -> str:
    """List PipeWire links in ID mode."""
    return await run_command("pw-link", "-Iil")


async def wpctl_set_volume(node_id: int, volume_linear: float) -> str:
    """Set node volume via wpctl. volume_linear is 0.0-1.0+."""
    vol_str = f"{volume_linear:.4f}"
    return await run_command("wpctl", "set-volume", str(node_id), vol_str)


# ── Parsers ──────────────────────────────────────────────────────────────

# pw-link -Iil output format:
#   <output_port_id>  <output_node>:<port_name>
#    |- <link_id> -> <input_port_id>  <input_node>:<port_name>
_LINK_OUTPUT_RE = re.compile(r"^\s+\|-\s+(\d+)\s+->\s+(\d+)\s+")
_PORT_LINE_RE = re.compile(r"^(\d+)\s+(.+)$")


def parse_pw_dump_nodes(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract PipeWire Node objects from pw-dump output."""
    nodes: list[dict[str, Any]] = []
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Node":
            info = obj.get("info", {})
            if not isinstance(info, dict):
                continue
            props = info.get("props", {})
            if not isinstance(props, dict):
                continue
            # Extract latency from params if available
            latency_ns = 0
            sample_rate = 0
            channels = 0
            params = info.get("params", {})
            if isinstance(params, dict):
                latency_list = params.get("Latency", [])
                if isinstance(latency_list, list) and latency_list:
                    lat = latency_list[0] if isinstance(latency_list[0], dict) else {}
                    latency_ns = lat.get("minNs", 0)
                fmt_list = params.get("EnumFormat", params.get("Format", []))
                if isinstance(fmt_list, list) and fmt_list:
                    fmt = fmt_list[0] if isinstance(fmt_list[0], dict) else {}
                    sample_rate = fmt.get("rate", 0)
                    channels = fmt.get("channels", 0)

            nodes.append(
                {
                    "id": obj.get("id", 0),
                    "name": props.get("node.name", ""),
                    "media_class": props.get("media.class", ""),
                    "nick": props.get("node.nick", props.get("node.description", "")),
                    "state": info.get("state", "unknown"),
                    "bt_codec": props.get("api.bluez5.codec", ""),
                    "bt_address": props.get("api.bluez5.address", ""),
                    "bt_profile": props.get("api.bluez5.profile", ""),
                    "latency_ms": round(latency_ns / 1_000_000, 1) if latency_ns else 0.0,
                    "sample_rate": sample_rate,
                    "channels": channels,
                }
            )
    return nodes


def parse_pw_dump_ports(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract PipeWire Port objects from pw-dump output."""
    ports: list[dict[str, Any]] = []
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Port":
            info = obj.get("info", {})
            if not isinstance(info, dict):
                continue
            props = info.get("props", {})
            if not isinstance(props, dict):
                continue
            direction_raw = info.get("direction", props.get("port.direction", ""))
            direction = "output" if direction_raw in ("output", "out") else "input"
            ports.append(
                {
                    "id": obj.get("id", 0),
                    "node_id": props.get("node.id", 0),
                    "name": props.get("port.name", ""),
                    "direction": direction,
                    "alias": props.get("port.alias", props.get("object.path", "")),
                }
            )
    return ports


def parse_pw_dump_links(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract PipeWire Link objects from pw-dump output."""
    links: list[dict[str, Any]] = []
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Link":
            info = obj.get("info", {})
            if not isinstance(info, dict):
                continue
            links.append(
                {
                    "id": obj.get("id", 0),
                    "output_port_id": info.get("output-port-id", 0),
                    "input_port_id": info.get("input-port-id", 0),
                    "state": info.get("state", "unknown"),
                }
            )
    return links
