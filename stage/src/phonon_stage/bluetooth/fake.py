"""Fake Bluetooth backend for tests — in-memory state management."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from phonon_stage.bluetooth.backend import BluetoothController, BluetoothDevice


class FakeBluetoothBackend:
    """Test double for BluetoothBackend with full operation support."""

    def __init__(
        self,
        controllers: list[BluetoothController] | None = None,
        devices: list[BluetoothDevice] | None = None,
    ) -> None:
        self._controllers = list(controllers or [])
        self._devices = list(devices or [])
        self._powered: dict[str, bool] = {}
        self._scan_results: list[BluetoothDevice] = list(devices or [])
        self.call_log: list[str] = []

    async def list_controllers(self) -> list[BluetoothController]:
        return list(self._controllers)

    async def set_alias(self, controller_address: str, alias: str) -> None:
        self.call_log.append(f"alias:{controller_address}:{alias}")

    async def set_power(self, controller_address: str, powered: bool) -> None:
        self._powered[controller_address] = powered
        self.call_log.append(f"power:{controller_address}:{powered}")

    async def set_role(self, controller_address: str, role: str) -> None:
        self.call_log.append(f"role:{controller_address}:{role}")

    async def start_scan(
        self, controller_address: str, timeout: float = 10.0
    ) -> list[BluetoothDevice]:
        self.call_log.append(f"scan:{controller_address}")
        return list(self._scan_results)

    async def pair(self, device_address: str) -> None:
        self.call_log.append(f"pair:{device_address}")

    async def unpair(self, device_address: str) -> None:
        self.call_log.append(f"unpair:{device_address}")
        self._devices = [d for d in self._devices if d.address != device_address]

    async def connect(self, device_address: str) -> None:
        self.call_log.append(f"connect:{device_address}")

    async def disconnect(self, device_address: str) -> None:
        self.call_log.append(f"disconnect:{device_address}")

    async def list_paired_devices(self, controller_address: str) -> list[BluetoothDevice]:
        return [d for d in self._devices if d.paired]
