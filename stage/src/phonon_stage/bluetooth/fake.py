"""Fake Bluetooth backend for tests — returns injectable controller list."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phonon_stage.bluetooth.backend import BluetoothController


class FakeBluetoothBackend:
    """Test double for BluetoothBackend. Inject controllers at construction."""

    def __init__(self, controllers: list[BluetoothController] | None = None) -> None:
        self._controllers = controllers or []

    async def list_controllers(self) -> list[BluetoothController]:
        return list(self._controllers)
