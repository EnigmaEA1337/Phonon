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

    async def start_scan(
        self, controller_address: str, timeout: float = 10.0
    ) -> list[BluetoothDevice]: ...

    async def pair(self, device_address: str) -> None: ...

    async def connect(self, device_address: str) -> None: ...

    async def disconnect(self, device_address: str) -> None: ...

    async def list_paired_devices(self, controller_address: str) -> list[BluetoothDevice]: ...
