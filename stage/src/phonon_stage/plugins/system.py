"""SystemBackend — abstracts systemd + filesystem operations for plugins.

All plugin-related OS-level state changes funnel through this module,
which lets us:
  * unit-test plugin logic without touching real systemd
  * keep `subprocess` calls localized (CLAUDE.md mandate: no scattered
    subprocess.run() in business logic)
  * mock filesystem writes during tests so we don't trash dev machines

Two implementations live here together because they're tightly coupled
(Real wraps the OS; Fake mirrors its semantics in-memory). Concrete
plugins depend only on the `SystemBackend` Protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()


class SystemBackendError(Exception):
    """Raised on systemctl / filesystem failures the plugin should surface."""


class SystemBackend(Protocol):
    """Operations a plugin needs from the host OS."""

    async def systemctl_is_enabled(self, unit: str) -> bool: ...

    async def systemctl_is_active(self, unit: str) -> bool: ...

    async def systemctl_enable(self, unit: str) -> None: ...

    async def systemctl_disable(self, unit: str) -> None: ...

    async def systemctl_start(self, unit: str) -> None: ...

    async def systemctl_stop(self, unit: str) -> None: ...

    async def systemctl_restart(self, unit: str) -> None: ...

    async def systemctl_status_stderr(self, unit: str) -> str:
        """Last ~10 lines of stderr/stdout from the unit, for diagnostics.
        Empty string if unavailable."""
        ...

    def read_text(self, path: Path) -> str: ...

    def write_text_atomic(self, path: Path, content: str, mode: int = 0o644) -> None: ...

    def file_exists(self, path: Path) -> bool: ...


class RealSystemBackend:
    """Drives the host's user-session systemd via `systemctl --user`
    (plugins run as the phonon user, no privileged escalation needed)
    and writes config files in `/var/lib/phonon/plugins/*/` which the
    units reference via absolute paths.

    On the production host phonon-stage runs as UID 999 (phonon user)
    with lingering enabled, so `systemctl --user` has a valid session
    bus to talk to even when nobody is logged in interactively.
    """

    SYSTEMCTL = "systemctl"
    USER_FLAG = "--user"

    async def _run(self, *args: str, expect_zero: bool = True) -> tuple[int, str, str]:
        """Run a subprocess, return (rc, stdout, stderr). Caller decides
        whether to raise on non-zero — `is-enabled`/`is-active` use the
        exit code as state, not as failure."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        rc = proc.returncode or 0
        if expect_zero and rc != 0:
            msg = f"{' '.join(args)} returned {rc}: {err.decode(errors='replace').strip()}"
            raise SystemBackendError(msg)
        return rc, out.decode(errors="replace"), err.decode(errors="replace")

    async def systemctl_is_enabled(self, unit: str) -> bool:
        _rc, out, _ = await self._run(
            self.SYSTEMCTL, self.USER_FLAG, "is-enabled", unit, expect_zero=False
        )
        # is-enabled prints "enabled", "static", "alias" → all considered
        # autostart-on for our purposes. "disabled", "masked", "linked",
        # "not-found" → not enabled. rc tracks the same, but parsing the
        # output is more explicit.
        state = out.strip()
        return state in {"enabled", "static", "alias", "enabled-runtime"}

    async def systemctl_is_active(self, unit: str) -> bool:
        _rc, out, _ = await self._run(
            self.SYSTEMCTL, self.USER_FLAG, "is-active", unit, expect_zero=False
        )
        return out.strip() == "active"

    async def systemctl_enable(self, unit: str) -> None:
        await self._run(self.SYSTEMCTL, self.USER_FLAG, "enable", unit)

    async def systemctl_disable(self, unit: str) -> None:
        await self._run(self.SYSTEMCTL, self.USER_FLAG, "disable", unit)

    async def systemctl_start(self, unit: str) -> None:
        await self._run(self.SYSTEMCTL, self.USER_FLAG, "start", unit)

    async def systemctl_stop(self, unit: str) -> None:
        await self._run(self.SYSTEMCTL, self.USER_FLAG, "stop", unit)

    async def systemctl_restart(self, unit: str) -> None:
        await self._run(self.SYSTEMCTL, self.USER_FLAG, "restart", unit)

    async def systemctl_status_stderr(self, unit: str) -> str:
        # `status` exits non-zero when the unit is inactive — we treat
        # that as informative, not as a backend failure, so expect_zero
        # stays False and we just take whatever output we got.
        _rc, out, _err = await self._run(
            self.SYSTEMCTL,
            self.USER_FLAG,
            "status",
            "--no-pager",
            "-n",
            "10",
            unit,
            expect_zero=False,
        )
        return out.strip()

    def read_text(self, path: Path) -> str:
        return path.read_text()

    def write_text_atomic(self, path: Path, content: str, mode: int = 0o644) -> None:
        """Write `content` to `path` via tmp+rename so readers never see
        a partial file. Creates the parent directory if missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # tempfile in the same directory so rename is atomic (cross-fs
        # rename would silently degrade to copy+delete and lose the
        # atomicity guarantee).
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.chmod(tmp_name, mode)
            os.replace(tmp_name, path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise

    def file_exists(self, path: Path) -> bool:
        return path.exists()


class FakeSystemBackend:
    """In-memory test double — units have explicit enabled/active flags,
    file writes land in a dict. Tests inspect both directly."""

    def __init__(self) -> None:
        self.enabled: dict[str, bool] = {}
        self.active: dict[str, bool] = {}
        self.files: dict[Path, str] = {}
        # Callable hooks so individual tests can simulate failures
        # (e.g. systemctl_enable raising for a unit that doesn't exist)
        # without monkey-patching the class.
        self.fail_on: set[tuple[str, str]] = set()  # (op, unit)

    def _maybe_fail(self, op: str, unit: str) -> None:
        if (op, unit) in self.fail_on:
            msg = f"FakeSystemBackend forced failure: {op} {unit}"
            raise SystemBackendError(msg)

    async def systemctl_is_enabled(self, unit: str) -> bool:
        return self.enabled.get(unit, False)

    async def systemctl_is_active(self, unit: str) -> bool:
        return self.active.get(unit, False)

    async def systemctl_enable(self, unit: str) -> None:
        self._maybe_fail("enable", unit)
        self.enabled[unit] = True

    async def systemctl_disable(self, unit: str) -> None:
        self._maybe_fail("disable", unit)
        self.enabled[unit] = False

    async def systemctl_start(self, unit: str) -> None:
        self._maybe_fail("start", unit)
        self.active[unit] = True

    async def systemctl_stop(self, unit: str) -> None:
        self._maybe_fail("stop", unit)
        self.active[unit] = False

    async def systemctl_restart(self, unit: str) -> None:
        self._maybe_fail("restart", unit)
        self.active[unit] = True

    async def systemctl_status_stderr(self, unit: str) -> str:
        return f"fake-status: {unit} active={self.active.get(unit, False)}"

    def read_text(self, path: Path) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_text_atomic(self, path: Path, content: str, mode: int = 0o644) -> None:
        self.files[path] = content

    def file_exists(self, path: Path) -> bool:
        return path in self.files
