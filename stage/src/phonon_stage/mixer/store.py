"""JSON persistence for the mix-console state.

The whole MixerState is read/written as a single blob — small enough
(tens of strips at most) that we don't bother with row-level updates.
Atomic tmp+rename on every write so a daemon crash mid-save doesn't
leave a half-written conf that fails to parse at the next boot.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import TYPE_CHECKING

from phonon_stage.mixer.models import MixerState

if TYPE_CHECKING:
    from pathlib import Path


class MixerStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._state: MixerState = MixerState()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def state(self) -> MixerState:
        return self._state

    def load(self) -> MixerState:
        """Load from disk into memory and return it. Missing or
        unparseable file → empty state (treated as first-boot)."""
        if not self._path.exists():
            self._state = MixerState()
            return self._state
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            self._state = MixerState()
            return self._state
        self._state = MixerState.from_dict(data)
        return self._state

    def save(self) -> None:
        """Persist the in-memory state. Atomic tmp+rename — readers
        never see a partial JSON file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", dir=str(self._path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._state.to_dict(), f, indent=2)
                f.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self._path)
        except Exception:
            import contextlib

            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def replace_state(self, new_state: MixerState) -> None:
        """Replace the in-memory state and immediately persist."""
        self._state = new_state
        self.save()
