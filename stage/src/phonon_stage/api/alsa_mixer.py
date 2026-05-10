"""ALSA hardware mixer endpoints.

PipeWire's per-stream / per-sink volume sits *above* the ALSA hardware
mixer level. They normally stay in sync via the HW_VOLUME_CTRL flag, but
when two PipeWire instances share the same hardware (manager + phonon
sessions) or after some BlueZ disconnect cycles, the two layers drift —
typical symptom: PipeWire reports vol 56% but the underlying DAC
control is at 0% and audio is silent.

The Phonon UI's faders manipulate the PipeWire side (mixing strip
gain). This module exposes the *hardware* mixer state separately so the
user can see and fix the discrepancy without dropping to a shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/audio/alsa", tags=["alsa-mixer"])
logger = structlog.get_logger()


class AlsaControl(BaseModel):
    """One simple mixer control on an ALSA card."""

    model_config = ConfigDict(extra="forbid")
    name: str
    has_playback: bool
    has_capture: bool
    has_pswitch: bool
    has_cswitch: bool
    volume_min: int = 0
    volume_max: int = 0
    volume_raw: int = 0  # current raw value (in [min, max])
    volume_pct: int = 0  # 0..100 derived from raw
    volume_db: float = 0.0  # current dB if reported by the driver
    muted: bool = False


class AlsaCard(BaseModel):
    """One ALSA card and its simple mixer controls."""

    model_config = ConfigDict(extra="forbid")
    card_id: int
    card_name: str  # short name, e.g. 'DG60'
    card_description: str  # human-friendly, e.g. 'Avantree DG60'
    controls: list[AlsaControl] = Field(default_factory=list)


class SetControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    volume_pct: int | None = Field(default=None, ge=0, le=100)
    muted: bool | None = None


async def _run(cmd: str, timeout: float = 4.0) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode(errors="ignore").strip()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return ""


# /proc/asound/cards entry shape:
#   N [shortname       ]: <driver> - <description>
#                           <bus path>
# We only need the short name (used by amixer -c) and a description.
_CARD_RE = re.compile(r"^\s*(\d+)\s*\[(\S+)\s*\]:\s*\S+\s*-\s*(.+?)\s*$")


async def _list_cards_raw() -> list[tuple[int, str, str]]:
    """Read /proc/asound/cards. Returns [(id, shortname, description)]."""
    raw = await _run("cat /proc/asound/cards")
    cards: list[tuple[int, str, str]] = []
    for line in raw.splitlines():
        m = _CARD_RE.match(line)
        if m:
            cards.append((int(m.group(1)), m.group(2), m.group(3)))
    return cards


# Parser for `amixer -c <id> scontents`. Each control block looks like:
#
#   Simple mixer control 'PCM',0
#     Capabilities: pvolume pvolume-joined pswitch pswitch-joined
#     Playback channels: Mono
#     Limits: Playback 0 - 15
#     Mono: Playback 11 [73%] [-4.00dB] [on]
#
# Capture-only or capture+playback controls are similar. We only return
# controls that have either pvolume OR cvolume — the rest (e.g. pure
# routing switches) aren't useful in the UI.

_CONTROL_HEADER = re.compile(r"^Simple mixer control '([^']+)',\d+\s*$")
_LIMITS = re.compile(r"^\s*Limits:\s*(?:Playback|Capture)\s+(-?\d+)\s*-\s*(-?\d+)\s*$")
# The channel-name prefix can contain spaces ('Front Left:') so we use
# `[^:\n]+?:` instead of `\S+:`. We anchor on the literal Playback/Capture
# token so we don't accidentally match informational lines like "Mono:".
_VAL_LINE = re.compile(
    r"^\s*[^:\n]+?:\s*(?:Playback|Capture)\s+(-?\d+)\s*"
    r"(?:\[(\d+)%\])?\s*"
    r"(?:\[(-?\d+\.\d+)dB\])?\s*"
    r"(?:\[(on|off)\])?\s*$"
)


def _parse_scontents(text: str) -> list[AlsaControl]:
    controls: list[AlsaControl] = []
    cur: dict[str, Any] | None = None
    caps: set[str] = set()
    for line in text.splitlines():
        m = _CONTROL_HEADER.match(line)
        if m:
            if cur is not None:
                controls.append(_finalize(cur, caps))
            cur = {"name": m.group(1)}
            caps = set()
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Capabilities:"):
            caps = set(s.split(":", 1)[1].split())
            continue
        m = _LIMITS.match(line)
        if m:
            cur["volume_min"] = int(m.group(1))
            cur["volume_max"] = int(m.group(2))
            continue
        m = _VAL_LINE.match(line)
        if m and "volume_raw" not in cur:
            cur["volume_raw"] = int(m.group(1))
            if m.group(2) is not None:
                cur["volume_pct"] = int(m.group(2))
            if m.group(3) is not None:
                cur["volume_db"] = float(m.group(3))
            cur["muted"] = m.group(4) == "off" if m.group(4) else False
    if cur is not None:
        controls.append(_finalize(cur, caps))
    return controls


def _finalize(cur: dict[str, Any], caps: set[str]) -> AlsaControl:
    # Compute pct from raw if amixer didn't surface it (some drivers omit
    # the [%] hint for tiny ranges).
    raw = int(cur.get("volume_raw", 0))
    vmin = int(cur.get("volume_min", 0))
    vmax = int(cur.get("volume_max", 0))
    pct = int(cur.get("volume_pct", 0))
    if pct == 0 and vmax > vmin:
        pct = max(0, min(100, round(100 * (raw - vmin) / (vmax - vmin))))
    return AlsaControl(
        name=str(cur.get("name", "")),
        has_playback="pvolume" in caps,
        has_capture="cvolume" in caps,
        has_pswitch="pswitch" in caps,
        has_cswitch="cswitch" in caps,
        volume_min=vmin,
        volume_max=vmax,
        volume_raw=raw,
        volume_pct=pct,
        volume_db=float(cur.get("volume_db", 0.0)),
        muted=bool(cur.get("muted", False)),
    )


async def _list_card_controls(card_id: int) -> list[AlsaControl]:
    raw = await _run(f"amixer -c {card_id} scontents 2>/dev/null")
    if not raw:
        return []
    controls = _parse_scontents(raw)
    # Hide controls that are purely 'switch' / routing. Keep anything with
    # a real volume range.
    return [c for c in controls if c.volume_max > c.volume_min]


@router.get("/cards", response_model=list[AlsaCard])
async def list_cards() -> list[AlsaCard]:
    """List every ALSA card on the host with its useful mixer controls."""
    cards = []
    for cid, short, desc in await _list_cards_raw():
        controls = await _list_card_controls(cid)
        cards.append(
            AlsaCard(card_id=cid, card_name=short, card_description=desc, controls=controls)
        )
    return cards


@router.get("/cards/{card}", response_model=AlsaCard)
async def get_card(card: str) -> AlsaCard:
    """Get one ALSA card by short name (e.g. 'DG60') or numeric id."""
    cards = await _list_cards_raw()
    target = next(
        ((cid, short, desc) for cid, short, desc in cards if short == card or str(cid) == card),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail=f"alsa card {card!r} not found")
    cid, short, desc = target
    return AlsaCard(
        card_id=cid,
        card_name=short,
        card_description=desc,
        controls=await _list_card_controls(cid),
    )


@router.put("/cards/{card}/controls/{control}", response_model=AlsaControl)
async def set_control(card: str, control: str, body: SetControlRequest) -> AlsaControl:
    """Set the volume and/or mute state on one mixer control.

    `card` is the ALSA short name ('DG60') or numeric id. `control` is the
    simple mixer control name ('PCM', 'Master'). Body fields are
    independent — set just one or both.
    """
    if body.volume_pct is None and body.muted is None:
        raise HTTPException(
            status_code=400,
            detail="must provide at least one of volume_pct, muted",
        )
    cards = await _list_cards_raw()
    target = next(
        ((cid, short, desc) for cid, short, desc in cards if short == card or str(cid) == card),
        None,
    )
    if target is None:
        raise HTTPException(status_code=404, detail=f"alsa card {card!r} not found")
    cid, _, _ = target

    # amixer accepts 'sset <name> <pct>%' for volume and 'sset <name> on|off'
    # for mute. We can chain both in one invocation.
    args = []
    if body.volume_pct is not None:
        args.append(f"{body.volume_pct}%")
    if body.muted is not None:
        args.append("off" if body.muted else "on")
    # Quote the control name to survive spaces (e.g. 'Master Front').
    out = await _run(f"amixer -c {cid} sset '{control}' {' '.join(args)} 2>&1")
    if "Invalid command" in out or "Unable to find" in out or "no such" in out.lower():
        logger.warning("alsa.set_control_failed", card=card, control=control, output=out)
        raise HTTPException(status_code=400, detail=f"amixer rejected: {out[:200]}")
    logger.info(
        "alsa.control_set",
        card=card,
        control=control,
        volume_pct=body.volume_pct,
        muted=body.muted,
    )
    # Return the post-set state so the UI can reflect what actually
    # took effect (raw value may differ from requested due to driver
    # rounding on small ranges like 0..15).
    controls = await _list_card_controls(cid)
    found = next((c for c in controls if c.name == control), None)
    if found is None:
        raise HTTPException(status_code=404, detail=f"control {control!r} disappeared post-set")
    return found
