"""Real Bluetooth backend — BlueZ adapter and device management via D-Bus."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog

from phonon_stage.bluetooth.backend import BluetoothController, BluetoothDevice

logger = structlog.get_logger()

_ADAPTER_INTERFACE = "org.bluez.Adapter1"
_DEVICE_INTERFACE = "org.bluez.Device1"


class RealBluetoothBackend:
    """Manage BT controllers and devices via BlueZ D-Bus."""

    async def _get_bus(self):  # type: ignore[no-untyped-def]  # dbus-fast dynamic types
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType

        return await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def _get_managed_objects(self, bus: object) -> Any:  # dbus-fast returns dynamic types
        introspection = await bus.introspect("org.bluez", "/")  # type: ignore[attr-defined]
        proxy = bus.get_proxy_object("org.bluez", "/", introspection)  # type: ignore[attr-defined]
        obj_manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        return await obj_manager.call_get_managed_objects()  # type: ignore[attr-defined]

    async def list_controllers(self) -> list[BluetoothController]:
        try:
            bus = await self._get_bus()
        except Exception:
            logger.warning("bluetooth.dbus_connection_failed", exc_info=True)
            return []

        try:
            objects = await self._get_managed_objects(bus)
            controllers: list[BluetoothController] = []
            for _path, interfaces in objects.items():
                if _ADAPTER_INTERFACE not in interfaces:
                    continue
                props = interfaces[_ADAPTER_INTERFACE]
                controllers.append(
                    BluetoothController(
                        address=props.get("Address", {}).value,
                        name=props.get("Name", {}).value,
                        alias=props.get("Alias", {}).value,
                        powered=props.get("Powered", {}).value,
                        discovering=props.get("Discovering", {}).value,
                    )
                )
            return controllers
        except Exception:
            logger.warning("bluetooth.enumeration_failed", exc_info=True)
            return []
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def _find_adapter_path(self, bus: object, controller_address: str) -> str:
        objects = await self._get_managed_objects(bus)
        for path, interfaces in objects.items():
            if _ADAPTER_INTERFACE in interfaces:
                props = interfaces[_ADAPTER_INTERFACE]
                if props.get("Address", {}).value == controller_address:
                    return path  # type: ignore[return-value]
        msg = f"Adapter {controller_address} not found"
        raise ValueError(msg)

    async def _find_device_path(self, bus: object, device_address: str) -> str:
        objects = await self._get_managed_objects(bus)
        for path, interfaces in objects.items():
            if _DEVICE_INTERFACE in interfaces:
                props = interfaces[_DEVICE_INTERFACE]
                if props.get("Address", {}).value == device_address:
                    return path  # type: ignore[return-value]
        msg = f"Device {device_address} not found"
        raise ValueError(msg)

    async def set_power(self, controller_address: str, powered: bool) -> None:
        bus = await self._get_bus()
        try:
            path = await self._find_adapter_path(bus, controller_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            props_iface = proxy.get_interface("org.freedesktop.DBus.Properties")
            from dbus_fast import Variant

            await props_iface.call_set(_ADAPTER_INTERFACE, "Powered", Variant("b", powered))
            logger.info("bluetooth.power_set", controller=controller_address, powered=powered)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def start_scan(
        self, controller_address: str, timeout: float = 10.0
    ) -> list[BluetoothDevice]:
        bus = await self._get_bus()
        try:
            path = await self._find_adapter_path(bus, controller_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            adapter = proxy.get_interface(_ADAPTER_INTERFACE)

            await adapter.call_start_discovery()  # type: ignore[attr-defined]
            await asyncio.sleep(timeout)
            with contextlib.suppress(Exception):
                await adapter.call_stop_discovery()  # type: ignore[attr-defined]

            return await self._list_devices(bus)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def pair(self, device_address: str) -> None:
        bus = await self._get_bus()
        try:
            path = await self._find_device_path(bus, device_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            device = proxy.get_interface(_DEVICE_INTERFACE)
            await device.call_pair()  # type: ignore[attr-defined]
            logger.info("bluetooth.paired", device=device_address)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def connect(self, device_address: str) -> None:
        bus = await self._get_bus()
        try:
            path = await self._find_device_path(bus, device_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            device = proxy.get_interface(_DEVICE_INTERFACE)
            await device.call_connect()  # type: ignore[attr-defined]
            logger.info("bluetooth.connected", device=device_address)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def disconnect(self, device_address: str) -> None:
        bus = await self._get_bus()
        try:
            path = await self._find_device_path(bus, device_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            device = proxy.get_interface(_DEVICE_INTERFACE)
            await device.call_disconnect()  # type: ignore[attr-defined]
            logger.info("bluetooth.disconnected", device=device_address)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def list_paired_devices(self, controller_address: str) -> list[BluetoothDevice]:
        bus = await self._get_bus()
        try:
            devices = await self._list_devices(bus)
            return [d for d in devices if d.paired]
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def _list_devices(self, bus: object) -> list[BluetoothDevice]:
        objects = await self._get_managed_objects(bus)
        devices: list[BluetoothDevice] = []
        for _path, interfaces in objects.items():
            if _DEVICE_INTERFACE not in interfaces:
                continue
            props = interfaces[_DEVICE_INTERFACE]
            devices.append(
                BluetoothDevice(
                    address=props.get("Address", {}).value,
                    name=props.get("Name", {}).value,
                    alias=props.get("Alias", {}).value,
                    paired=props.get("Paired", {}).value,
                    connected=props.get("Connected", {}).value,
                    icon=props.get("Icon", {}).value if "Icon" in props else "audio-card",
                )
            )
        return devices
