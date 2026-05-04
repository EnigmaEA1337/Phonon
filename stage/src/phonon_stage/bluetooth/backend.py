"""Bluetooth backend protocol and domain models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BluetoothController:
    """A Bluetooth adapter detected via BlueZ."""

    address: str
    name: str
    alias: str
    powered: bool
    discovering: bool
    discoverable: bool = False
    pairable: bool = False
    hw_name: str = ""  # USB product name (e.g. "ASUS BCM20702A0", "CSR Dongle")


@dataclass(frozen=True)
class BluetoothDevice:
    """A Bluetooth device discovered or paired via BlueZ."""

    address: str
    name: str
    alias: str
    paired: bool
    connected: bool
    icon: str  # "audio-card", "phone", etc.


class BluetoothBackend(Protocol):
    """Protocol for Bluetooth controller and device management."""

    async def list_controllers(self) -> list[BluetoothController]: ...

    async def set_power(self, controller_address: str, powered: bool) -> None: ...

    async def set_alias(self, controller_address: str, alias: str) -> None: ...

    async def open_pairing_window(self, controller_address: str, duration: int = 60) -> None: ...

    async def set_role(self, controller_address: str, role: str) -> None: ...

    async def start_scan(
        self, controller_address: str, timeout: float = 10.0
    ) -> list[BluetoothDevice]: ...

    async def pair(self, device_address: str) -> None: ...

    async def unpair(self, device_address: str) -> None: ...

    async def connect(self, device_address: str) -> None: ...

    async def disconnect(self, device_address: str) -> None: ...

    async def list_paired_devices(self, controller_address: str) -> list[BluetoothDevice]: ...
