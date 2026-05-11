"""Tests for /dsp endpoints and the plugin-insert paths under /mixer."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from phonon_stage.pipewire.backend import PwNode, PwPort

if TYPE_CHECKING:
    from httpx import AsyncClient

    from phonon_stage.pipewire.fake import FakePipeWireBackend


LSP_LIBRARY = "lsp-plugins-ladspa"
LSP_LABEL = "http://lsp-plug.in/plugins/ladspa/comp_delay_stereo"


async def _make_output_with_sink(
    client: AsyncClient,
    fake_pw: FakePipeWireBackend,
    *,
    node_id: int,
    sink_name: str,
    label: str,
) -> str:
    """Inject a sink+ports into the fake PW graph, POST a mixer output
    pointing at it, and return the created output's id. Centralising
    this setup keeps the individual tests readable."""
    fake_pw.nodes.append(
        PwNode(
            id=node_id,
            name=sink_name,
            media_class="Audio/Sink",
            nick=label,
            state="idle",
        )
    )
    base_port = node_id * 10
    fake_pw.ports.extend(
        [
            PwPort(
                id=base_port,
                node_id=node_id,
                name="playback_FL",
                direction="input",
                alias=f"{label}:FL",
            ),
            PwPort(
                id=base_port + 1,
                node_id=node_id,
                name="playback_FR",
                direction="input",
                alias=f"{label}:FR",
            ),
        ]
    )
    r = await client.post(
        "/mixer/outputs",
        json={"sink_node_name": sink_name, "label": label},
    )
    assert r.status_code == 201
    return str(r.json()["id"])


async def _attach_lsp_delay(client: AsyncClient, output_id: str) -> None:
    r = await client.patch(
        f"/mixer/outputs/{output_id}/insert",
        json={"backend": "ladspa", "library": LSP_LIBRARY, "label": LSP_LABEL},
    )
    assert r.status_code == 200


# ── /capabilities flag ────────────────────────────────────────────────


class TestCapabilitiesFlag:
    @pytest.mark.asyncio()
    async def test_plugins_available_true_when_introspector_wired(
        self, client: AsyncClient
    ) -> None:
        r = await client.get("/capabilities")
        assert r.status_code == 200
        assert r.json()["plugins_available"] is True


# ── /dsp/plugins ──────────────────────────────────────────────────────


class TestDspCatalog:
    @pytest.mark.asyncio()
    async def test_list_returns_v1_catalog(self, client: AsyncClient) -> None:
        r = await client.get("/dsp/plugins")
        assert r.status_code == 200
        plugins = r.json()
        assert isinstance(plugins, list)
        assert any(p["label"] == LSP_LABEL for p in plugins)

    @pytest.mark.asyncio()
    async def test_schema_returns_controls(self, client: AsyncClient) -> None:
        r = await client.get(
            "/dsp/plugins/schema",
            params={"library": LSP_LIBRARY, "label": LSP_LABEL},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["library"] == LSP_LIBRARY
        assert body["label"] == LSP_LABEL
        names = [c["name"] for c in body["controls"]]
        # Both audio + control ports come back; UI filters direction.
        assert "Time (ms)" in names
        assert "Mode" in names
        assert "Input L" in names

    @pytest.mark.asyncio()
    async def test_schema_unknown_plugin_404(self, client: AsyncClient) -> None:
        r = await client.get(
            "/dsp/plugins/schema",
            params={"library": "nope", "label": "nope"},
        )
        assert r.status_code == 404


# ── PATCH /mixer/outputs/{id}/insert ──────────────────────────────────


class TestOutputInsertSet:
    @pytest.mark.asyncio()
    async def test_attach_seeds_defaults(
        self, client: AsyncClient, fake_pw: FakePipeWireBackend
    ) -> None:
        out_id = await _make_output_with_sink(
            client, fake_pw, node_id=99, sink_name="alsa_output.test", label="T"
        )
        r = await client.patch(
            f"/mixer/outputs/{out_id}/insert",
            json={"backend": "ladspa", "library": LSP_LIBRARY, "label": LSP_LABEL},
        )
        assert r.status_code == 200
        ins = r.json()["insert"]
        assert ins is not None
        assert ins["enabled"] is True
        assert ins["controls"]["Time (ms)"] == 5.0
        assert ins["controls"]["Mode"] == 2.0

    @pytest.mark.asyncio()
    async def test_clear_with_all_null(
        self, client: AsyncClient, fake_pw: FakePipeWireBackend
    ) -> None:
        out_id = await _make_output_with_sink(
            client, fake_pw, node_id=98, sink_name="alsa_output.test2", label="T2"
        )
        await _attach_lsp_delay(client, out_id)
        r = await client.patch(
            f"/mixer/outputs/{out_id}/insert",
            json={"backend": None, "library": None, "label": None},
        )
        assert r.status_code == 200
        assert r.json()["insert"] is None

    @pytest.mark.asyncio()
    async def test_unknown_backend_rejected(self, client: AsyncClient) -> None:
        # Validation runs before the service is reached so no output
        # is required.
        r = await client.patch(
            "/mixer/outputs/missing/insert",
            json={"backend": "lv2", "library": "x", "label": "y"},
        )
        assert r.status_code == 400
        assert "backend" in r.json()["detail"]

    @pytest.mark.asyncio()
    async def test_half_set_rejected(self, client: AsyncClient) -> None:
        r = await client.patch(
            "/mixer/outputs/anything/insert",
            json={"backend": "ladspa", "library": None, "label": None},
        )
        assert r.status_code == 400


# ── PATCH .../insert/controls/{name} ──────────────────────────────────


class TestInsertControlUpdate:
    @pytest.mark.asyncio()
    async def test_live_update_returns_new_value(
        self, client: AsyncClient, fake_pw: FakePipeWireBackend
    ) -> None:
        out_id = await _make_output_with_sink(
            client, fake_pw, node_id=97, sink_name="alsa_output.test3", label="T3"
        )
        await _attach_lsp_delay(client, out_id)

        r = await client.patch(
            f"/mixer/outputs/{out_id}/insert/controls/Time (ms)",
            json={"value": 80.0},
        )
        assert r.status_code == 200
        assert r.json()["insert"]["controls"]["Time (ms)"] == 80.0
        # Backend was told the new value live (no reload).
        assert fake_pw.filter_chain_controls[(f"phonon_fx_{out_id}", "Time (ms)")] == 80.0

    @pytest.mark.asyncio()
    async def test_unknown_control_404(
        self, client: AsyncClient, fake_pw: FakePipeWireBackend
    ) -> None:
        out_id = await _make_output_with_sink(
            client, fake_pw, node_id=96, sink_name="alsa_output.test4", label="T4"
        )
        await _attach_lsp_delay(client, out_id)
        r = await client.patch(
            f"/mixer/outputs/{out_id}/insert/controls/Nonsense",
            json={"value": 1.0},
        )
        assert r.status_code == 404
