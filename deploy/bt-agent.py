#!/usr/bin/env python3
"""Phonon BlueZ Agent — auto-accepts pairing and auto-trusts devices."""
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

AGENT_INTERFACE = "org.bluez.Agent1"
AGENT_PATH = "/phonon/agent"

class Agent(dbus.service.Object):
    def _trust_device(self, device_path):
        """Auto-trust any device that connects."""
        try:
            bus = dbus.SystemBus()
            props = dbus.Interface(
                bus.get_object("org.bluez", device_path),
                "org.freedesktop.DBus.Properties"
            )
            props.Set("org.bluez.Device1", "Trusted", True)
            print(f"Auto-trusted: {device_path}")
        except Exception as e:
            print(f"Trust failed: {e}")

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        print(f"AuthorizeService: {device} {uuid} -> auto-accept")
        self._trust_device(device)

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        self._trust_device(device)
        return "0000"

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self._trust_device(device)
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_INTERFACE, in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        pass

    @dbus.service.method(AGENT_INTERFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        print(f"RequestConfirmation: {device} passkey={passkey} -> auto-confirm")
        self._trust_device(device)

    @dbus.service.method(AGENT_INTERFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        print(f"RequestAuthorization: {device} -> auto-authorize")
        self._trust_device(device)

    @dbus.service.method(AGENT_INTERFACE, in_signature="", out_signature="")
    def Cancel(self):
        pass

if __name__ == "__main__":
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    agent = Agent(bus, AGENT_PATH)
    manager = dbus.Interface(
        bus.get_object("org.bluez", "/org/bluez"),
        "org.bluez.AgentManager1"
    )
    manager.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    manager.RequestDefaultAgent(AGENT_PATH)
    print("Phonon BT Agent running (auto-accept + auto-trust)", flush=True)
    GLib.MainLoop().run()
