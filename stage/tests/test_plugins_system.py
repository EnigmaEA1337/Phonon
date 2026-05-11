"""Tests for SystemBackend — FakeSystemBackend semantics + RealSystemBackend atomicity."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.plugins.system import (
    FakeSystemBackend,
    RealSystemBackend,
    SystemBackendError,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestFakeSystemBackend:
    @pytest.fixture()
    def sb(self) -> FakeSystemBackend:
        return FakeSystemBackend()

    async def test_initial_state(self, sb: FakeSystemBackend) -> None:
        assert await sb.systemctl_is_enabled("anything.service") is False
        assert await sb.systemctl_is_active("anything.service") is False

    async def test_enable_then_disable(self, sb: FakeSystemBackend) -> None:
        await sb.systemctl_enable("foo.service")
        assert await sb.systemctl_is_enabled("foo.service") is True
        await sb.systemctl_disable("foo.service")
        assert await sb.systemctl_is_enabled("foo.service") is False

    async def test_start_stop_restart(self, sb: FakeSystemBackend) -> None:
        await sb.systemctl_start("foo.service")
        assert await sb.systemctl_is_active("foo.service") is True
        await sb.systemctl_stop("foo.service")
        assert await sb.systemctl_is_active("foo.service") is False
        await sb.systemctl_restart("foo.service")
        assert await sb.systemctl_is_active("foo.service") is True

    async def test_enable_doesnt_imply_active(self, sb: FakeSystemBackend) -> None:
        """Enabled = autostart-at-boot, not running-now. The plugin's
        runtime() relies on them being independent — keep them so in
        the fake."""
        await sb.systemctl_enable("foo.service")
        assert await sb.systemctl_is_enabled("foo.service") is True
        assert await sb.systemctl_is_active("foo.service") is False

    async def test_fail_on_simulates_systemctl_failure(self, sb: FakeSystemBackend) -> None:
        sb.fail_on.add(("enable", "broken.service"))
        with pytest.raises(SystemBackendError, match="enable broken"):
            await sb.systemctl_enable("broken.service")

    def test_write_and_read_text(self, sb: FakeSystemBackend, tmp_path: Path) -> None:
        target = tmp_path / "conf"
        sb.write_text_atomic(target, "hello")
        assert sb.file_exists(target) is True
        assert sb.read_text(target) == "hello"


class TestRealSystemBackendAtomicWrite:
    """The atomic file write is the only Real piece worth integration-
    testing in-process — systemctl is mocked elsewhere. A partial-file
    visibility bug here would corrupt shairport-sync conf and brick a
    plugin on first config save."""

    def test_atomic_write_creates_parent(self, tmp_path: Path) -> None:
        rb = RealSystemBackend()
        target = tmp_path / "nested" / "deeper" / "conf"
        rb.write_text_atomic(target, "payload")
        assert target.read_text() == "payload"

    def test_atomic_write_replaces_existing(self, tmp_path: Path) -> None:
        rb = RealSystemBackend()
        target = tmp_path / "conf"
        target.write_text("old")
        rb.write_text_atomic(target, "new")
        assert target.read_text() == "new"

    def test_atomic_write_no_partial_tmp_left(self, tmp_path: Path) -> None:
        rb = RealSystemBackend()
        target = tmp_path / "conf"
        rb.write_text_atomic(target, "payload")
        # No .tmp / .conf.* lingering after a successful write
        leftovers = [p for p in tmp_path.iterdir() if p.name != target.name]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"

    def test_atomic_write_sets_mode(self, tmp_path: Path) -> None:
        rb = RealSystemBackend()
        target = tmp_path / "conf"
        rb.write_text_atomic(target, "x", mode=0o600)
        assert target.stat().st_mode & 0o777 == 0o600
