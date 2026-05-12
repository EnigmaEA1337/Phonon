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
    MacvlanConfig,
    NetworkState,
    VlanConfig,
    _parse_ss_ptp,
    _valid_iface_name,
    _valid_ipv4,
    _valid_ipv4_cidr,
    _validate_iface_cfg,
    _validate_macvlan_cfg,
    _validate_vlan_cfg,
    parse_ethtool_T,
    render_macvlan_netdev,
    render_macvlan_network,
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


# ─── macvlan validator + renderer (slice 8) ──────────────


def test_validate_macvlan_dhcp_passes():
    cfg = MacvlanConfig(parent="enp1s0", dhcp4=True)
    assert _validate_macvlan_cfg("mvl-airplay", cfg) is None


def test_validate_macvlan_static_full():
    cfg = MacvlanConfig(
        parent="enp1s0", mac="aa:bb:cc:dd:ee:ff",
        dhcp4=False, addresses4=["192.168.1.50/24"], gateway4="192.168.1.254",
        dns=["1.1.1.1"], mtu=1500,
    )
    assert _validate_macvlan_cfg("mvl-aes67", cfg) is None


def test_validate_macvlan_rejects_self_parent():
    cfg = MacvlanConfig(parent="mvl-foo", dhcp4=True)
    err = _validate_macvlan_cfg("mvl-foo", cfg)
    assert err and "same name" in err


def test_validate_macvlan_rejects_bad_mac():
    for bad in ("zz:zz:zz:zz:zz:zz", "aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee", "ggggg"):
        cfg = MacvlanConfig(parent="enp1s0", mac=bad)
        err = _validate_macvlan_cfg("mvl-x", cfg)
        assert err and "MAC" in err, f"expected MAC error for {bad!r}"


def test_validate_macvlan_static_requires_address():
    cfg = MacvlanConfig(parent="enp1s0", dhcp4=False)
    err = _validate_macvlan_cfg("mvl-x", cfg)
    assert err and "address" in err


def test_render_macvlan_netdev_dhcp_basic():
    cfg = MacvlanConfig(parent="enp1s0", dhcp4=True)
    body = render_macvlan_netdev("mvl-airplay", cfg)
    assert "Kind=macvlan" in body
    assert "Name=mvl-airplay" in body
    assert "Mode=bridge" in body
    # No MAC line when MAC is empty (kernel auto-assigns).
    assert "MACAddress" not in body


def test_render_macvlan_netdev_with_mac():
    cfg = MacvlanConfig(parent="enp1s0", mac="aa:bb:cc:dd:ee:ff", dhcp4=True)
    body = render_macvlan_netdev("mvl-x", cfg)
    assert "MACAddress=aa:bb:cc:dd:ee:ff" in body


def test_render_macvlan_network_static():
    cfg = MacvlanConfig(
        parent="enp1s0", dhcp4=False, addresses4=["192.168.1.50/24"],
        gateway4="192.168.1.254", dns=["1.1.1.1", "8.8.8.8"],
    )
    body = render_macvlan_network("mvl-x", cfg)
    assert "[Match]" in body
    assert "Name=mvl-x" in body
    assert "DHCP=ipv4" not in body
    assert "Address=192.168.1.50/24" in body
    assert "Gateway=192.168.1.254" in body
    assert "DNS=1.1.1.1" in body
    assert "DNS=8.8.8.8" in body


def test_render_macvlan_network_dhcp():
    cfg = MacvlanConfig(parent="enp1s0", dhcp4=True)
    body = render_macvlan_network("mvl-x", cfg)
    assert "DHCP=ipv4" in body
    assert "Address=" not in body


# ─── ss -tulnp parser (PTP socket bindings) ──────────────

# Captured verbatim from stage-x99 during the slice 8 nqptp tests.
SS_OUTPUT_BOTH_RUNNING = """\
udp   UNCONN 0      0               0.0.0.0%enp1s0:319        0.0.0.0:*    users:(("ptp4l",pid=1032120,fd=13))
udp   UNCONN 0      0               0.0.0.0%enp1s0:320        0.0.0.0:*    users:(("ptp4l",pid=1032120,fd=14))
udp   UNCONN 0      0                         [::]:319           [::]:*    users:(("nqptp",pid=1028515,fd=5))
udp   UNCONN 0      0                         [::]:320           [::]:*    users:(("nqptp",pid=1028515,fd=7))
"""


def test_parse_ss_extracts_ptp4l_and_nqptp():
    rows = _parse_ss_ptp(SS_OUTPUT_BOTH_RUNNING)
    assert len(rows) == 4
    ptp4l = [r for r in rows if r.daemon == "ptp4l"]
    nqptp = [r for r in rows if r.daemon == "nqptp"]
    assert len(ptp4l) == 2 and len(nqptp) == 2
    # ptp4l is BINDTODEVICE'd to enp1s0
    assert all(r.iface == "enp1s0" for r in ptp4l)
    assert all(r.address == "0.0.0.0" for r in ptp4l)
    # nqptp is IPv6 wildcard, no iface
    assert all(r.iface == "" for r in nqptp)
    assert all(r.address == "::" for r in nqptp)
    # Ports 319 and 320 represented once each per daemon
    assert sorted(r.port for r in ptp4l) == [319, 320]
    assert sorted(r.port for r in nqptp) == [319, 320]


def test_parse_ss_ignores_non_ptp_ports():
    # If the helper ever stops grep-filtering, the parser still
    # ignores anything outside 319/320.
    extra = SS_OUTPUT_BOTH_RUNNING + (
        "udp   UNCONN 0 0  0.0.0.0:5353  0.0.0.0:*  users:((\"avahi-daemon\",pid=999,fd=1))\n"
    )
    rows = _parse_ss_ptp(extra)
    assert all(r.port in (319, 320) for r in rows)


def test_parse_ss_handles_missing_users_column():
    # Non-root ss output may omit the process column for foreign-uid
    # daemons. Parser should still extract addr + port.
    text = "udp   UNCONN 0      0          0.0.0.0%mvl-ptp:319    0.0.0.0:*\n"
    rows = _parse_ss_ptp(text)
    assert len(rows) == 1
    assert rows[0].daemon == "unknown"
    assert rows[0].pid == 0
    assert rows[0].iface == "mvl-ptp"
    assert rows[0].port == 319


def test_parse_ss_empty_input():
    assert _parse_ss_ptp("") == []
    assert _parse_ss_ptp("\n\n") == []
