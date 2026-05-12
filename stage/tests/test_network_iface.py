"""Unit tests for the iface config validators + netplan YAML renderer
in api/network.py.

Hardware-free: we just feed config objects into the validator + render
the YAML body that would land in /etc/netplan/99-phonon-managed.yaml.
The actual netplan / networkctl path is tested manually on the Pi.
"""

from __future__ import annotations

import pytest

from phonon_stage.api.network import (
    IfaceConfig,
    NetworkState,
    VlanConfig,
    _valid_iface_name,
    _valid_ipv4,
    _valid_ipv4_cidr,
    _validate_iface_cfg,
    _validate_vlan_cfg,
    parse_ethtool_T,
    render_netplan_yaml,
)


def _single(name: str, cfg: IfaceConfig) -> NetworkState:
    """Helper — wrap one iface into a NetworkState for the renderer."""
    return NetworkState(ethernets={name: cfg})


# ─── validators ──────────────────────────────────────────


def test_valid_ipv4_accepts_normal_addrs():
    assert _valid_ipv4("192.168.1.21")
    assert _valid_ipv4("10.0.0.1")
    assert _valid_ipv4("0.0.0.0")
    assert _valid_ipv4("255.255.255.255")


def test_valid_ipv4_rejects_garbage():
    assert not _valid_ipv4("")
    assert not _valid_ipv4("256.1.1.1")
    assert not _valid_ipv4("192.168.1")
    assert not _valid_ipv4("192.168.1.1.1")
    assert not _valid_ipv4("foo.bar.baz.qux")
    assert not _valid_ipv4("192.168.1.21/24")  # CIDR, not bare IP


def test_valid_ipv4_cidr_accepts_normal():
    assert _valid_ipv4_cidr("192.168.1.21/24")
    assert _valid_ipv4_cidr("10.0.0.1/8")
    assert _valid_ipv4_cidr("0.0.0.0/0")
    assert _valid_ipv4_cidr("255.255.255.255/32")


def test_valid_ipv4_cidr_rejects_garbage():
    assert not _valid_ipv4_cidr("192.168.1.21")        # no prefix
    assert not _valid_ipv4_cidr("192.168.1.21/33")     # prefix > 32
    assert not _valid_ipv4_cidr("256.1.1.1/24")        # bad octet
    assert not _valid_ipv4_cidr("")


def test_valid_iface_name_accepts_real_names():
    for name in ("enp1s0", "eth0", "wlan0", "eth0.10", "br-lan", "phonon_v"):
        assert _valid_iface_name(name), name


def test_valid_iface_name_rejects_garbage():
    # Linux iface names are capped at 15 chars and forbid shell metas.
    assert not _valid_iface_name("")
    assert not _valid_iface_name("a" * 16)
    assert not _valid_iface_name("eth0;rm -rf /")
    assert not _valid_iface_name("eth0 with spaces")
    assert not _valid_iface_name("eth0`whoami`")


# ─── iface config validation ─────────────────────────────


def test_validate_dhcp_config_passes():
    assert _validate_iface_cfg("enp1s0", IfaceConfig(dhcp4=True)) is None


def test_validate_static_requires_address():
    err = _validate_iface_cfg("enp1s0", IfaceConfig(dhcp4=False))
    assert err is not None
    assert "address" in err


def test_validate_static_accepts_cidr_only():
    cfg = IfaceConfig(dhcp4=False, addresses4=["192.168.1.21/24"])
    assert _validate_iface_cfg("enp1s0", cfg) is None


def test_validate_accepts_multiple_addresses():
    cfg = IfaceConfig(
        dhcp4=False, addresses4=["192.168.1.21/24", "192.168.1.50/24"]
    )
    assert _validate_iface_cfg("enp1s0", cfg) is None


def test_validate_rejects_too_many_addresses():
    cfg = IfaceConfig(addresses4=[f"10.0.0.{i}/24" for i in range(1, 10)])
    err = _validate_iface_cfg("enp1s0", cfg)
    assert err and "too many" in err


def test_validate_rejects_bare_ip_in_addresses():
    cfg = IfaceConfig(dhcp4=False, addresses4=["192.168.1.21"])  # missing prefix
    err = _validate_iface_cfg("enp1s0", cfg)
    assert err is not None
    assert "CIDR" in err


def test_validate_rejects_bad_gateway():
    cfg = IfaceConfig(dhcp4=False, addresses4=["192.168.1.21/24"], gateway4="not-an-ip")
    err = _validate_iface_cfg("enp1s0", cfg)
    assert err is not None
    assert "gateway" in err


def test_validate_rejects_bad_dns():
    cfg = IfaceConfig(dns=["1.1.1.1", "not-an-ip"])
    err = _validate_iface_cfg("enp1s0", cfg)
    assert err is not None
    assert "DNS" in err


def test_validate_rejects_bad_iface_name():
    err = _validate_iface_cfg("eth0;rm", IfaceConfig())
    assert err is not None
    assert "iface name" in err


def test_validate_mtu_range():
    assert _validate_iface_cfg("eth0", IfaceConfig(mtu=1500)) is None
    assert _validate_iface_cfg("eth0", IfaceConfig(mtu=9216)) is None
    err = _validate_iface_cfg("eth0", IfaceConfig(mtu=500))
    assert err and "MTU" in err
    err = _validate_iface_cfg("eth0", IfaceConfig(mtu=10000))
    assert err and "MTU" in err


def test_validate_timeout_range():
    assert _validate_iface_cfg("eth0", IfaceConfig(timeout_s=120)) is None
    err = _validate_iface_cfg("eth0", IfaceConfig(timeout_s=10))
    assert err and "timeout" in err
    err = _validate_iface_cfg("eth0", IfaceConfig(timeout_s=900))
    assert err and "timeout" in err


# ─── VLAN config validation ──────────────────────────────


def test_validate_vlan_dhcp_passes():
    cfg = VlanConfig(parent="enp1s0", vlan_id=10, dhcp4=True)
    assert _validate_vlan_cfg("enp1s0.10", cfg) is None


def test_validate_vlan_static_full():
    cfg = VlanConfig(
        parent="enp1s0", vlan_id=42,
        dhcp4=False, addresses4=["10.42.0.21/24"], gateway4="10.42.0.1",
    )
    assert _validate_vlan_cfg("enp1s0.42", cfg) is None


def test_validate_vlan_id_out_of_range():
    for bad in (0, 4095, 5000, -1):
        cfg = VlanConfig(parent="enp1s0", vlan_id=bad)
        err = _validate_vlan_cfg(f"enp1s0.{bad}", cfg)
        assert err and "VLAN id" in err


def test_validate_vlan_rejects_bad_parent():
    cfg = VlanConfig(parent="eth0;rm", vlan_id=10)
    err = _validate_vlan_cfg("enp1s0.10", cfg)
    assert err and "parent" in err


def test_validate_vlan_static_requires_address():
    cfg = VlanConfig(parent="enp1s0", vlan_id=10, dhcp4=False)
    err = _validate_vlan_cfg("enp1s0.10", cfg)
    assert err and "address" in err


# ─── YAML renderer ───────────────────────────────────────


def test_render_dhcp_minimal():
    yaml = render_netplan_yaml(_single("enp1s0", IfaceConfig(dhcp4=True)))
    assert "version: 2" in yaml
    assert "renderer: networkd" in yaml
    assert "enp1s0:" in yaml
    assert "dhcp4: true" in yaml
    # No addresses / routes blocks on a pure DHCP config without aliases.
    assert "addresses:" not in yaml
    assert "routes:" not in yaml


def test_render_static_full():
    cfg = IfaceConfig(
        dhcp4=False,
        addresses4=["10.0.0.21/24"],
        gateway4="10.0.0.1",
        dns=["1.1.1.1", "8.8.8.8"],
        mtu=1500,
    )
    yaml = render_netplan_yaml(_single("enp1s0", cfg))
    assert "dhcp4: false" in yaml
    assert "addresses: [10.0.0.21/24]" in yaml
    assert "to: default" in yaml
    assert "via: 10.0.0.1" in yaml
    # DNS uses a separate addresses key under nameservers — distinct
    # from the iface-level addresses; check both surfaces are present.
    assert "nameservers:" in yaml
    assert "addresses: [1.1.1.1, 8.8.8.8]" in yaml
    assert "mtu: 1500" in yaml


def test_render_aliases_as_address_list():
    # The point of slice 5 — multiple IPv4 addresses on one iface
    # become one comma-joined `addresses:` line in YAML.
    cfg = IfaceConfig(
        dhcp4=False,
        addresses4=["10.0.0.21/24", "10.0.0.50/24", "10.0.0.51/24"],
    )
    yaml = render_netplan_yaml(_single("enp1s0", cfg))
    assert "addresses: [10.0.0.21/24, 10.0.0.50/24, 10.0.0.51/24]" in yaml


def test_render_aliases_alongside_dhcp():
    # netplan accepts manual addresses on top of a DHCP-leased iface —
    # the manuals act as aliases additive to the DHCP lease.
    cfg = IfaceConfig(dhcp4=True, addresses4=["10.0.0.50/24"])
    yaml = render_netplan_yaml(_single("enp1s0", cfg))
    assert "dhcp4: true" in yaml
    assert "addresses: [10.0.0.50/24]" in yaml


def test_render_dns_on_dhcp():
    cfg = IfaceConfig(dhcp4=True, dns=["1.1.1.1"])
    yaml = render_netplan_yaml(_single("enp1s0", cfg))
    assert "dhcp4: true" in yaml
    assert "nameservers:" in yaml
    assert "addresses: [1.1.1.1]" in yaml


def test_render_no_gateway_when_omitted():
    cfg = IfaceConfig(dhcp4=False, addresses4=["10.0.0.21/24"])
    yaml = render_netplan_yaml(_single("enp1s0", cfg))
    assert "routes:" not in yaml
    assert "to: default" not in yaml


def test_render_managed_header():
    yaml = render_netplan_yaml(_single("eth0", IfaceConfig()))
    assert yaml.startswith("# Managed by phonon-stage")


def test_render_multiple_ifaces():
    state = NetworkState(ethernets={
        "enp1s0": IfaceConfig(dhcp4=True),
        "enp2s0": IfaceConfig(dhcp4=False, addresses4=["10.0.0.10/24"]),
    })
    yaml = render_netplan_yaml(state)
    assert "enp1s0:" in yaml
    assert "enp2s0:" in yaml
    assert "10.0.0.10/24" in yaml


def test_render_vlan_block():
    state = NetworkState(
        ethernets={"enp1s0": IfaceConfig(dhcp4=True)},
        vlans={
            "enp1s0.10": VlanConfig(
                parent="enp1s0", vlan_id=10,
                dhcp4=False, addresses4=["10.10.0.21/24"], gateway4="10.10.0.1",
            ),
        },
    )
    yaml = render_netplan_yaml(state)
    assert "vlans:" in yaml
    assert "enp1s0.10:" in yaml
    assert "id: 10" in yaml
    assert "link: enp1s0" in yaml
    assert "addresses: [10.10.0.21/24]" in yaml


def test_render_empty_state():
    # Empty state still yields a parseable netplan body (header only)
    # so the applier never feeds netplan an invalid file.
    yaml = render_netplan_yaml(NetworkState())
    assert "network:" in yaml
    assert "version: 2" in yaml
    # No section headers without content
    assert "ethernets:" not in yaml
    assert "vlans:" not in yaml


# ─── ethtool -T parser ───────────────────────────────────

# Full HW-capable NIC — Intel I210/I350 typical output. Has all three
# hardware flags + a real PHC index.
ETHTOOL_HW_CAPABLE = """\
Time stamping parameters for enp1s0:
Capabilities:
        hardware-transmit
        software-transmit
        hardware-receive
        software-receive
        software-system-clock
        hardware-raw-clock
PTP Hardware Clock: 0
Hardware Transmit Timestamp Modes:
        off                   (HWTSTAMP_TX_OFF)
        on                    (HWTSTAMP_TX_ON)
Hardware Receive Filter Modes:
        none                  (HWTSTAMP_FILTER_NONE)
        all                   (HWTSTAMP_FILTER_ALL)
"""

# Software-only NIC — typical Realtek r8169 (the Pi's enp1s0 on the
# 3070, USB-Eth dongles). No hardware flags, no PHC.
ETHTOOL_SW_ONLY = """\
Time stamping parameters for enp1s0:
Capabilities:
        software-transmit
        software-receive
        software-system-clock
PTP Hardware Clock: none
Hardware Transmit Timestamp Modes:
        off                   (HWTSTAMP_TX_OFF)
Hardware Receive Filter Modes:
        none                  (HWTSTAMP_FILTER_NONE)
"""

# Legacy ethtool — uses SOF_TIMESTAMPING_* constants instead of the
# keyword form. Same NIC semantics as HW_CAPABLE above.
ETHTOOL_LEGACY_FLAGS = """\
Time stamping parameters for eth0:
Capabilities:
        SOF_TIMESTAMPING_TX_HARDWARE
        SOF_TIMESTAMPING_TX_SOFTWARE
        SOF_TIMESTAMPING_RX_HARDWARE
        SOF_TIMESTAMPING_RX_SOFTWARE
        SOF_TIMESTAMPING_RAW_HARDWARE
        SOF_TIMESTAMPING_SOFTWARE
PTP Hardware Clock: 2
"""


def test_ethtool_parses_hw_capable_nic():
    c = parse_ethtool_T(ETHTOOL_HW_CAPABLE)
    assert c.hw_transmit
    assert c.hw_receive
    assert c.hw_raw_clock
    assert c.sw_transmit
    assert c.sw_receive
    assert c.sw_system_clock
    assert c.phc_index == 0
    assert c.hw_ptp_capable is True
    assert c.raw_available is True


def test_ethtool_parses_sw_only_nic():
    c = parse_ethtool_T(ETHTOOL_SW_ONLY)
    assert not c.hw_transmit
    assert not c.hw_receive
    assert not c.hw_raw_clock
    assert c.sw_transmit
    assert c.sw_receive
    assert c.sw_system_clock
    # "none" → -1
    assert c.phc_index == -1
    assert c.hw_ptp_capable is False


def test_ethtool_accepts_legacy_sof_flags():
    c = parse_ethtool_T(ETHTOOL_LEGACY_FLAGS)
    assert c.hw_transmit
    assert c.hw_receive
    assert c.hw_raw_clock
    # phc_index=2 — confirms we parse beyond just "0"
    assert c.phc_index == 2
    assert c.hw_ptp_capable is True


def test_ethtool_empty_input_flags_unavailable():
    c = parse_ethtool_T("")
    assert c.raw_available is False
    assert c.hw_ptp_capable is False
    assert c.phc_index == -1


def test_ethtool_hw_ptp_requires_phc_not_just_caps():
    # NIC reports HW-capable flags but no PHC — happens on weird
    # drivers that lie about caps. We must reject it for the
    # hw_ptp_capable badge so ptp4l isn't pointed at a bogus iface.
    fake = ETHTOOL_HW_CAPABLE.replace("PTP Hardware Clock: 0", "PTP Hardware Clock: none")
    c = parse_ethtool_T(fake)
    assert c.hw_transmit
    assert c.hw_receive
    assert c.phc_index == -1
    assert c.hw_ptp_capable is False
