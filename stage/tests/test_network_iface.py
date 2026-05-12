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
    _valid_iface_name,
    _valid_ipv4,
    _valid_ipv4_cidr,
    _validate_config,
    render_netplan_yaml,
)


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


# ─── full config validation ──────────────────────────────


def test_validate_dhcp_config_passes():
    assert _validate_config("enp1s0", IfaceConfig(dhcp4=True)) is None


def test_validate_static_requires_address4():
    err = _validate_config("enp1s0", IfaceConfig(dhcp4=False))
    assert err is not None
    assert "address4" in err


def test_validate_static_accepts_cidr_only():
    cfg = IfaceConfig(dhcp4=False, address4="192.168.1.21/24")
    assert _validate_config("enp1s0", cfg) is None


def test_validate_static_rejects_bare_ip():
    cfg = IfaceConfig(dhcp4=False, address4="192.168.1.21")  # missing prefix
    err = _validate_config("enp1s0", cfg)
    assert err is not None
    assert "CIDR" in err


def test_validate_rejects_bad_gateway():
    cfg = IfaceConfig(dhcp4=False, address4="192.168.1.21/24", gateway4="not-an-ip")
    err = _validate_config("enp1s0", cfg)
    assert err is not None
    assert "gateway" in err


def test_validate_rejects_bad_dns():
    cfg = IfaceConfig(dns=["1.1.1.1", "not-an-ip"])
    err = _validate_config("enp1s0", cfg)
    assert err is not None
    assert "DNS" in err


def test_validate_rejects_bad_iface_name():
    err = _validate_config("eth0;rm", IfaceConfig())
    assert err is not None
    assert "iface name" in err


def test_validate_mtu_range():
    assert _validate_config("eth0", IfaceConfig(mtu=1500)) is None
    assert _validate_config("eth0", IfaceConfig(mtu=9216)) is None
    err = _validate_config("eth0", IfaceConfig(mtu=500))
    assert err and "MTU" in err
    err = _validate_config("eth0", IfaceConfig(mtu=10000))
    assert err and "MTU" in err


def test_validate_timeout_range():
    assert _validate_config("eth0", IfaceConfig(timeout_s=120)) is None
    err = _validate_config("eth0", IfaceConfig(timeout_s=10))
    assert err and "timeout" in err
    err = _validate_config("eth0", IfaceConfig(timeout_s=900))
    assert err and "timeout" in err


# ─── YAML renderer ───────────────────────────────────────


def test_render_dhcp_minimal():
    yaml = render_netplan_yaml({"enp1s0": IfaceConfig(dhcp4=True)})
    assert "version: 2" in yaml
    assert "renderer: networkd" in yaml
    assert "enp1s0:" in yaml
    assert "dhcp4: true" in yaml
    # Static-only fields must be absent on a pure DHCP config.
    assert "addresses:" not in yaml
    assert "routes:" not in yaml


def test_render_static_full():
    cfg = IfaceConfig(
        dhcp4=False,
        address4="10.0.0.21/24",
        gateway4="10.0.0.1",
        dns=["1.1.1.1", "8.8.8.8"],
        mtu=1500,
    )
    yaml = render_netplan_yaml({"enp1s0": cfg})
    assert "dhcp4: false" in yaml
    assert "addresses: [10.0.0.21/24]" in yaml
    assert "to: default" in yaml
    assert "via: 10.0.0.1" in yaml
    assert "addresses: [1.1.1.1, 8.8.8.8]" in yaml
    assert "mtu: 1500" in yaml


def test_render_dns_on_dhcp():
    # DHCP-leased iface that overrides DNS — netplan accepts this and
    # treats the manual DNS as additional/overriding.
    cfg = IfaceConfig(dhcp4=True, dns=["1.1.1.1"])
    yaml = render_netplan_yaml({"enp1s0": cfg})
    assert "dhcp4: true" in yaml
    assert "addresses: [1.1.1.1]" in yaml


def test_render_no_gateway_when_omitted():
    cfg = IfaceConfig(dhcp4=False, address4="10.0.0.21/24")
    yaml = render_netplan_yaml({"enp1s0": cfg})
    assert "routes:" not in yaml
    assert "to: default" not in yaml


def test_render_managed_header():
    yaml = render_netplan_yaml({"eth0": IfaceConfig()})
    assert yaml.startswith("# Managed by phonon-stage")


def test_render_multiple_ifaces():
    yaml = render_netplan_yaml({
        "enp1s0": IfaceConfig(dhcp4=True),
        "enp2s0": IfaceConfig(dhcp4=False, address4="10.0.0.10/24"),
    })
    assert "enp1s0:" in yaml
    assert "enp2s0:" in yaml
    assert "10.0.0.10/24" in yaml
