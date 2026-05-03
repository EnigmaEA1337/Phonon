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


def _prop(props: Any, key: str, default: Any = "") -> Any:
    """Extract value from D-Bus props (handles both Variant and raw types)."""
    val = props.get(key)
    if val is None:
        return default
    return val.value if hasattr(val, "value") else val


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
                        address=_prop(props, "Address"),
                        name=_prop(props, "Name"),
                        alias=_prop(props, "Alias"),
                        powered=_prop(props, "Powered", False),
                        discovering=_prop(props, "Discovering", False),
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
                if _prop(props, "Address") == controller_address:
                    return path  # type: ignore[return-value]
        msg = f"Adapter {controller_address} not found"
        raise ValueError(msg)

    async def _find_device_path(self, bus: object, device_address: str) -> str:
        objects = await self._get_managed_objects(bus)
        for path, interfaces in objects.items():
            if _DEVICE_INTERFACE in interfaces:
                props = interfaces[_DEVICE_INTERFACE]
                if _prop(props, "Address") == device_address:
                    return path  # type: ignore[return-value]
        msg = f"Device {device_address} not found"
        raise ValueError(msg)

    async def set_power(self, controller_address: str, powered: bool) -> None:
        # Select the right controller then power on/off
        await self._bluetoothctl("select", controller_address)
        state = "on" if powered else "off"
        await self._bluetoothctl("power", state)
        logger.info("bluetooth.power_set", controller=controller_address, powered=powered)

    async def start_scan(
        self, controller_address: str, timeout: float = 10.0
    ) -> list[BluetoothDevice]:
        bus = await self._get_bus()
        try:
            # Select controller and start scan via bluetoothctl
            await self._bluetoothctl("select", controller_address)
            await self._bluetoothctl("scan", "on")
            await asyncio.sleep(timeout)
            with contextlib.suppress(Exception):
                await self._bluetoothctl("scan", "off")

            return await self._list_devices(bus)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def pair(self, device_address: str) -> None:
        await self._bluetoothctl("trust", device_address)
        await self._bluetoothctl("pair", device_address)
        logger.info("bluetooth.paired", device=device_address)

    async def _bluetoothctl(self, *args: str) -> str:
        """Run bluetoothctl command via subprocess (more reliable than D-Bus for A2DP)."""
        cmd = ["bluetoothctl", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        output = stdout.decode().strip()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.warning("bluetooth.cmd_failed", cmd=args, stderr=err)
        return output

    async def unpair(self, device_address: str) -> None:
        await self._bluetoothctl("remove", device_address)
        logger.info("bluetooth.unpaired", device=device_address)

    async def connect(self, device_address: str) -> None:
        result = await self._bluetoothctl("connect", device_address)
        logger.info("bluetooth.connected", device=device_address, result=result)

    async def disconnect(self, device_address: str) -> None:
        result = await self._bluetoothctl("disconnect", device_address)
        logger.info("bluetooth.disconnected", device=device_address, result=result)

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
                    address=_prop(props, "Address"),
                    name=_prop(props, "Name"),
                    alias=_prop(props, "Alias"),
                    paired=_prop(props, "Paired", False),
                    connected=_prop(props, "Connected", False),
                    icon=_prop(props, "Icon", "audio-card"),
                )
            )
        return devices
