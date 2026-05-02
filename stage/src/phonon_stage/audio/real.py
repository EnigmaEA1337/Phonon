"""Real audio backend — enumerates ALSA devices via /proc/asound/."""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from phonon_stage.audio.backend import AudioDevice

logger = structlog.get_logger()

# Regex for /proc/asound/cards lines:
#  0 [bcm2835ALSA    ]: bcm2835_alsa - bcm2835 ALSA
_CARD_RE = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s+(\S+)\s+-\s+(.+)$")


class RealAudioBackend:
    """Enumerate audio devices by parsing /proc/asound/ (zero subprocess)."""

    def __init__(self, proc_asound: Path = Path("/proc/asound")) -> None:
        self._proc_asound = proc_asound

    async def list_devices(self) -> list[AudioDevice]:
        cards_path = self._proc_asound / "cards"
        if not cards_path.exists():
            logger.warning("audio.no_proc_asound", path=str(cards_path))
            return []

        text = cards_path.read_text()
        devices: list[AudioDevice] = []

        for line in text.splitlines():
            match = _CARD_RE.match(line)
            if not match:
                continue

            card_index = int(match.group(1))
            card_id = match.group(2)
            driver = match.group(3)
            name = match.group(4).strip()

            card_dir = self._proc_asound / f"card{card_index}"
            playback = any(card_dir.glob("pcm*p"))
            capture = any(card_dir.glob("pcm*c"))

            devices.append(
                AudioDevice(
                    card_index=card_index,
                    name=name,
                    id=card_id,
                    driver=driver,
                    playback=playback,
                    capture=capture,
                )
            )

        logger.info("audio.devices_enumerated", count=len(devices))
        return devices
