"""PipeWire subprocess wrapper — the ONLY module that calls subprocess.

All PipeWire CLI interactions (pw-cli, pw-link, wpctl) are isolated here.
No other module should import asyncio.create_subprocess_exec for PipeWire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
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

    # Force C locale so error messages are stable English (pw-link's
    # "File exists" check is locale-sensitive otherwise).
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
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


_pw_dump_cache: list[dict[str, Any]] = []
_pw_dump_cache_time: float = 0.0
_PW_DUMP_CACHE_TTL = 5.0  # seconds


async def pw_dump() -> list[dict[str, Any]]:
    """Run pw-dump and return parsed JSON array of PipeWire objects.

    Results are cached for 5 seconds to reduce subprocess overhead.
    pw-dump may stream continuously (monitoring mode). We read with a
    short timeout — once the initial dump is sent we parse whatever we got.
    """
    global _pw_dump_cache, _pw_dump_cache_time
    now = asyncio.get_event_loop().time()
    if _pw_dump_cache and (now - _pw_dump_cache_time) < _PW_DUMP_CACHE_TTL:
        return _pw_dump_cache
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
        import os

        logger.warning(
            "pipewire.pw_dump_empty",
            stderr=stderr,
            returncode=proc.returncode,
            xdg_runtime_dir=os.environ.get("XDG_RUNTIME_DIR", "NOT SET"),
        )
        raise PipeWireCliError("pw-dump", proc.returncode or -1, stderr or "no output")

    full = b"".join(chunks).decode()

    # pw-dump in monitoring mode may emit multiple successive JSON arrays
    # (one per state change). On a busy PipeWire session this happens within
    # the 2s read window, producing concatenated arrays. Keep only the first
    # complete array — that's the initial full dump.
    try:
        result = json.loads(full)
    except json.JSONDecodeError:
        first_close = full.find("\n]\n")
        if first_close == -1:
            first_close = full.find("\n]")
        if first_close >= 0:
            truncated = full[: first_close + 2]
            result = json.loads(truncated)
        else:
            raise PipeWireCliError("pw-dump", -1, "unparseable output") from None

    _pw_dump_cache = result
    _pw_dump_cache_time = asyncio.get_event_loop().time()
    return result  # type: ignore[no-any-return]


_pw_top_cache: dict[int, dict[str, Any]] = {}
_pw_top_cache_time: float = 0.0
_PW_TOP_CACHE_TTL = 3.0


async def pw_top_xruns() -> dict[int, dict[str, Any]]:
    """Run `pw-top -b` briefly and parse the latest snapshot's ERR column.

    Returns: {node_id: {name, err, state, format}}
    """
    global _pw_top_cache, _pw_top_cache_time
    now = asyncio.get_event_loop().time()
    if _pw_top_cache and (now - _pw_top_cache_time) < _PW_TOP_CACHE_TTL:
        return _pw_top_cache

    # `pw-top -b` (batch mode) without -n waits forever for a TTY-like stdin
    # and exits immediately when stdin is piped — yielding no output. The
    # trick is `-n 2` (or more) which makes it emit N snapshots and exit
    # cleanly. Two snapshots take ~2s; we get one steady-state for free.
    proc = await asyncio.create_subprocess_exec(
        "pw-top",
        "-b",
        "-n",
        "2",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=4.0)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning("pipewire.pw_top_timeout")
        return {}

    text = stdout_bytes.decode(errors="ignore")
    if not text.strip():
        logger.warning("pipewire.pw_top_empty")
        return {}
    logger.debug("pipewire.pw_top_raw", bytes=len(text), preview=text[:200])

    # Use the header line's column positions to locate NAME so we don't have
    # to fight the variable-width FORMAT column ("S16LE 2 44100" vs "---").
    # pw-top -b emits a fresh snapshot every second; we may have killed the
    # process mid-snapshot, so the LAST snapshot can be truncated. Pick the
    # snapshot with the most lines (the latest fully-emitted one).
    header_marker = "S   ID  QUANT"
    positions = []
    start = 0
    while True:
        idx = text.find(header_marker, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + 1
    if not positions:
        return {}

    snapshots = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        block = text[pos:end].splitlines()
        snapshots.append(block)
    # Pick the most populated block (filters out a truncated trailing one)
    snapshot = max(snapshots, key=len)
    if len(snapshot) < 2:
        return {}
    header_line = snapshot[0]
    name_col = header_line.find("NAME")
    if name_col < 0:
        return {}

    result: dict[int, dict[str, Any]] = {}
    for line in snapshot[1:]:
        if not line.strip() or line.startswith(header_marker):
            continue
        # Pad so column-slice is safe
        padded = line if len(line) >= name_col else line.ljust(name_col)
        prefix = padded[:name_col]
        name_part = padded[name_col:].strip()
        # Strip the leading "+ " of client-stream rows
        if name_part.startswith("+ "):
            name_part = name_part[2:].strip()
        parts = prefix.split()
        if len(parts) < 9:
            continue
        try:
            state = parts[0]
            node_id = int(parts[1])
            err = int(parts[8])
        except (ValueError, IndexError):
            continue
        if not name_part:
            continue
        result[node_id] = {"name": name_part, "err": err, "state": state}

    _pw_top_cache = result
    _pw_top_cache_time = now
    return result


async def pw_link_create(output_port_id: int, input_port_id: int) -> str:
    """Create a PipeWire link. Returns raw output. Ignores 'File exists' (already linked)."""
    try:
        return await run_command("pw-link", str(output_port_id), str(input_port_id))
    except PipeWireCliError as e:
        if "File exists" in e.stderr:
            return ""  # Link already exists, that's fine
        raise


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


async def pw_cli_set_latency_offset(node_id: int, offset_ns: int) -> str:
    """Set latency offset on a node via pw-cli. Used for output sync/phasing."""
    props_json = json.dumps({"latencyOffsetNsec": offset_ns})
    return await run_command("pw-cli", "set-param", str(node_id), "Props", props_json)


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
                    rate_raw = fmt.get("rate", 0)
                    sample_rate = (
                        rate_raw.get("default", 0) if isinstance(rate_raw, dict) else rate_raw
                    )
                    ch_raw = fmt.get("channels", 0)
                    channels = ch_raw.get("default", 0) if isinstance(ch_raw, dict) else ch_raw

            # ALSA backing: api.alsa.card.name is the human-friendly card
            # name ('Avantree DG60'); we normalize to the short alsa name
            # ('DG60') because amixer addresses cards by that. PipeWire
            # exposes both — prefer api.alsa.card.name fallback to alsa.card.
            alsa_card = (
                props.get("alsa.card_name", "")
                or props.get("alsa.card", "")
                or props.get("api.alsa.card.name", "")
            )
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
                    "alsa_card": alsa_card,
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
