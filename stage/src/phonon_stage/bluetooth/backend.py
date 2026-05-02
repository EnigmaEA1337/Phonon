"""Bluetooth backend protocol and domain model."""

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


class BluetoothBackend(Protocol):
    """Protocol for Bluetooth controller enumeration (read-only)."""

    async def list_controllers(self) -> list[BluetoothController]: ...
