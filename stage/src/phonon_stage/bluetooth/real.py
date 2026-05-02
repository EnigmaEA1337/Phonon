"""Real Bluetooth backend — enumerates BlueZ adapters via D-Bus."""

from __future__ import annotations

import structlog

from phonon_stage.bluetooth.backend import BluetoothController

logger = structlog.get_logger()

_ADAPTER_INTERFACE = "org.bluez.Adapter1"


class RealBluetoothBackend:
    """Enumerate BT controllers via BlueZ D-Bus ObjectManager (read-only)."""

    async def list_controllers(self) -> list[BluetoothController]:
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast.constants import BusType
        except ImportError:
            logger.warning("bluetooth.dbus_fast_not_available")
            return []

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception:
            logger.warning("bluetooth.dbus_connection_failed", exc_info=True)
            return []

        try:
            introspection = await bus.introspect("org.bluez", "/")
            proxy = bus.get_proxy_object("org.bluez", "/", introspection)
            obj_manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = await obj_manager.call_get_managed_objects()  # type: ignore[attr-defined]  # dbus-fast dynamic proxy

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

            logger.info("bluetooth.controllers_enumerated", count=len(controllers))
            return controllers
        except Exception:
            logger.warning("bluetooth.enumeration_failed", exc_info=True)
            return []
        finally:
            bus.disconnect()
