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

    async def _get_usb_hw_names(self) -> dict[str, str]:
        """Map BT adapter addresses to USB hardware product names via lsusb + hciconfig."""
        hw_map: dict[str, str] = {}
        try:
            # Get USB BT devices from lsusb
            proc = await asyncio.create_subprocess_exec(
                "lsusb", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
            usb_devices: dict[str, str] = {}
            for line in stdout.decode().splitlines():
                # "Bus 001 Device 015: ID 0b05:17cb ASUSTek Computer, Inc. Broadcom BCM20702A0"
                parts = line.split("ID ")
                if len(parts) < 2:
                    continue
                rest = parts[1]
                vid_pid = rest[:9]  # "0b05:17cb"
                desc = rest[10:].strip()
                if any(kw in desc.lower() for kw in ("bluetooth", "csr", "radio")):
                    usb_devices[vid_pid] = desc

            # Match hci adapters to USB vendor:product via sysfs
            from pathlib import Path

            for hci_dir in sorted(Path("/sys/class/bluetooth").glob("hci*")):
                hci_name = hci_dir.name
                # Get MAC address
                proc2 = await asyncio.create_subprocess_exec(
                    "hciconfig",
                    hci_name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=2.0)
                addr = ""
                for hline in out2.decode().splitlines():
                    if "BD Address" in hline:
                        addr = hline.split("BD Address:")[1].strip().split()[0]
                        break
                if not addr:
                    continue
                # Get USB vendor:product from sysfs
                uevent = hci_dir / "device" / "uevent"
                if uevent.exists():
                    for uline in uevent.read_text().splitlines():
                        if uline.startswith("PRODUCT="):
                            # PRODUCT=a12/1/134 → vid=0a12, pid=0001
                            pparts = uline.split("=")[1].split("/")
                            if len(pparts) >= 2:
                                vid = pparts[0].zfill(4)
                                pid = pparts[1].zfill(4)
                                key = f"{vid}:{pid}"
                                hw_map[addr] = usb_devices.get(key, f"USB {key}")
        except Exception:
            logger.debug("bluetooth.hw_names_failed", exc_info=True)
        return hw_map

    async def list_controllers(self) -> list[BluetoothController]:
        try:
            bus = await self._get_bus()
        except Exception:
            logger.warning("bluetooth.dbus_connection_failed", exc_info=True)
            return []

        try:
            hw_names = await self._get_usb_hw_names()
            objects = await self._get_managed_objects(bus)
            controllers: list[BluetoothController] = []
            for _path, interfaces in objects.items():
                if _ADAPTER_INTERFACE not in interfaces:
                    continue
                props = interfaces[_ADAPTER_INTERFACE]
                addr = _prop(props, "Address")
                controllers.append(
                    BluetoothController(
                        address=addr,
                        name=_prop(props, "Name"),
                        alias=_prop(props, "Alias"),
                        powered=_prop(props, "Powered", False),
                        discovering=_prop(props, "Discovering", False),
                        discoverable=_prop(props, "Discoverable", False),
                        pairable=_prop(props, "Pairable", False),
                        hw_name=hw_names.get(addr, ""),
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

    async def _find_hci_and_rfkill(self, controller_address: str) -> tuple[str, str]:
        """Find hci name and rfkill index for a controller by MAC address."""
        from pathlib import Path

        for hci_dir in sorted(Path("/sys/class/bluetooth").glob("hci*")):
            hci_name = hci_dir.name
            proc = await asyncio.create_subprocess_exec(
                "hciconfig",
                hci_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            if controller_address in out.decode():
                # Find rfkill index
                rfkill_path = hci_dir / "rfkill"
                if rfkill_path.exists():
                    name_file = rfkill_path / "name"
                    if name_file.exists():
                        rfk_name = name_file.read_text().strip()
                        # Get index from /sys/class/rfkill
                        for rf in Path("/sys/class/rfkill").glob("rfkill*"):
                            if (rf / "name").read_text().strip() == rfk_name:
                                return hci_name, rf.name.replace("rfkill", "")
                # Fallback: parse rfkill list
                proc2 = await asyncio.create_subprocess_exec(
                    "rfkill",
                    "list",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=2.0)
                for line in out2.decode().splitlines():
                    if hci_name in line and ":" in line:
                        return hci_name, line.split(":")[0].strip()
                return hci_name, ""
        return "", ""

    async def set_power(self, controller_address: str, powered: bool) -> None:
        hci, rfk_idx = await self._find_hci_and_rfkill(controller_address)
        if powered:
            if rfk_idx:
                await self._run_root("rfkill", "unblock", rfk_idx)
            if hci:
                await self._run_root("hciconfig", hci, "up")
        else:
            if hci:
                await self._run_root("hciconfig", hci, "down")
            if rfk_idx:
                await self._run_root("rfkill", "block", rfk_idx)
        logger.info("bluetooth.power_set", controller=controller_address, powered=powered, hci=hci)

    async def set_role(self, controller_address: str, role: str) -> None:
        """Set adapter role: 'receiver' (discoverable+pairable) or 'transmitter'."""
        bus = await self._get_bus()
        try:
            from dbus_fast import Variant

            path = await self._find_adapter_path(bus, controller_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            props = proxy.get_interface("org.freedesktop.DBus.Properties")

            is_receiver = role == "receiver"
            await props.call_set(_ADAPTER_INTERFACE, "Discoverable", Variant("b", is_receiver))
            await props.call_set(_ADAPTER_INTERFACE, "Pairable", Variant("b", is_receiver))
            if is_receiver:
                await props.call_set(_ADAPTER_INTERFACE, "DiscoverableTimeout", Variant("u", 0))
            logger.info("bluetooth.role_set", controller=controller_address, role=role)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def _run_root(self, *args: str) -> str:
        """Run a command that may need root (via sudo if available)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            # Try with sudo
            proc2 = await asyncio.create_subprocess_exec(
                "sudo",
                "-n",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc2.communicate(), timeout=5.0)
        return out.decode().strip()

    async def start_scan(
        self, controller_address: str, timeout: float = 10.0
    ) -> list[BluetoothDevice]:
        bus = await self._get_bus()
        try:
            # Use D-Bus for scan (bluetoothctl select doesn't persist between calls)
            path = await self._find_adapter_path(bus, controller_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            adapter = proxy.get_interface(_ADAPTER_INTERFACE)

            await adapter.call_start_discovery()  # type: ignore[attr-defined]
            await asyncio.sleep(timeout)
            with contextlib.suppress(Exception):
                await adapter.call_stop_discovery()  # type: ignore[attr-defined]

            # Only return devices seen by THIS adapter
            adapter_path = await self._find_adapter_path(bus, controller_address)
            return await self._list_devices(bus, adapter_path)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def pair(self, device_address: str) -> None:
        bus = await self._get_bus()
        try:
            path = await self._find_device_path(bus, device_address)
            introspection = await bus.introspect("org.bluez", path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", path, introspection)  # type: ignore[attr-defined]
            # Trust first
            from dbus_fast import Variant

            props = proxy.get_interface("org.freedesktop.DBus.Properties")
            await props.call_set(_DEVICE_INTERFACE, "Trusted", Variant("b", True))
            # Pair
            device = proxy.get_interface(_DEVICE_INTERFACE)
            await device.call_pair()  # type: ignore[attr-defined]
            logger.info("bluetooth.paired", device=device_address)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

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
        bus = await self._get_bus()
        try:
            device_path = await self._find_device_path(bus, device_address)
            adapter_path = "/".join(device_path.split("/")[:-1])
            introspection = await bus.introspect("org.bluez", adapter_path)  # type: ignore[attr-defined]
            proxy = bus.get_proxy_object("org.bluez", adapter_path, introspection)  # type: ignore[attr-defined]
            adapter = proxy.get_interface(_ADAPTER_INTERFACE)
            await adapter.call_remove_device(device_path)  # type: ignore[attr-defined]
            logger.info("bluetooth.unpaired", device=device_address)
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def _bluetoothctl_interactive(self, *commands: str, timeout: float = 15.0) -> str:
        """Run multiple bluetoothctl commands in a single interactive session."""
        script = "\n".join(commands) + "\n"
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=script.encode()), timeout=timeout
            )
            return stdout.decode()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ""

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
            adapter_path = await self._find_adapter_path(bus, controller_address)
            devices = await self._list_devices(bus, adapter_path)
            return [d for d in devices if d.paired]
        finally:
            bus.disconnect()  # type: ignore[attr-defined]

    async def _list_devices(
        self, bus: object, adapter_path: str | None = None
    ) -> list[BluetoothDevice]:
        objects = await self._get_managed_objects(bus)
        devices: list[BluetoothDevice] = []
        for _path, interfaces in objects.items():
            if _DEVICE_INTERFACE not in interfaces:
                continue
            # Filter by adapter: device path starts with adapter path
            if adapter_path and not str(_path).startswith(adapter_path):
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
