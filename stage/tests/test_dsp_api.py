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


# ── listplugins catalog parser ──────────────────────────────────────


class TestListpluginsParser:
    """Coverage for parse_listplugins_output, the dynamic catalog
    scan that powers the FX picker. Uses fixture strings shaped like
    real `listplugins` output (sampled from prod hosts)."""

    def test_parses_lsp_and_filters_test_categories(self) -> None:
        from phonon_stage.dsp.ladspa import parse_listplugins_output

        sample = """\
/usr/lib/ladspa/lsp-plugins-ladspa.so:
\tA/B Tester x2 Stereo (5002217/http://lsp-plug.in/plugins/ladspa/ab_tester_x2_stereo)
\tArtistic Delay Stereo (5002171/http://lsp-plug.in/plugins/ladspa/art_delay_stereo)
\tCompressor Stereo (5002233/http://lsp-plug.in/plugins/ladspa/comp_stereo)
\tParametric Equalizer x16 Stereo (5002188/http://lsp-plug.in/plugins/ladspa/para_equalizer_x16_stereo)
\tSpectrum Analyzer Stereo (5002277/http://lsp-plug.in/plugins/ladspa/spectrum_analyzer_stereo)
"""
        entries = parse_listplugins_output(sample)
        labels = [e.label for e in entries]
        # A/B tester (Test category) and Spectrum (Analysis) filtered out.
        assert all("ab_tester" not in label for label in labels)
        assert all("spectrum" not in label for label in labels)
        # Real effects kept.
        assert any("art_delay_stereo" in label for label in labels)
        assert any("comp_stereo" in label for label in labels)
        assert any("para_equalizer" in label for label in labels)

    def test_categorisation_heuristic(self) -> None:
        from phonon_stage.dsp.ladspa import parse_listplugins_output

        sample = """\
/usr/lib/ladspa/lsp-plugins-ladspa.so:
\tCompressor Stereo (5002233/http://lsp-plug.in/plugins/ladspa/comp_stereo)
\tArtistic Delay Stereo (5002171/http://lsp-plug.in/plugins/ladspa/art_delay_stereo)
\tParametric Equalizer x16 Stereo (5002188/http://lsp-plug.in/plugins/ladspa/para_equalizer_x16_stereo)
\tChorus Stereo (5002315/http://lsp-plug.in/plugins/ladspa/chorus_stereo)
"""
        entries = parse_listplugins_output(sample)
        by_label = {e.label.split("/")[-1]: e.category for e in entries}
        assert by_label["comp_stereo"] == "Dynamics"
        assert by_label["art_delay_stereo"] == "Time"
        assert by_label["para_equalizer_x16_stereo"] == "EQ"
        assert by_label["chorus_stereo"] == "Modulation"

    def test_library_extracted_from_path(self) -> None:
        from phonon_stage.dsp.ladspa import parse_listplugins_output

        sample = """\
/usr/lib/ladspa/lsp-plugins-ladspa.so:
\tDelay Compensator (Stereo) (5002274/http://lsp-plug.in/plugins/ladspa/comp_delay_stereo)
/usr/lib/ladspa/delay.so:
\tSimple Delay Line (1043/delay_5s)
"""
        entries = parse_listplugins_output(sample)
        libs = {e.library for e in entries}
        # Library is the basename minus .so — matches what filter-chain
        # passes to LADSPA's loader.
        assert libs == {"lsp-plugins-ladspa", "delay"}

    def test_stereo_flag_set_from_name(self) -> None:
        from phonon_stage.dsp.ladspa import parse_listplugins_output

        sample = """\
/usr/lib/ladspa/lsp-plugins-ladspa.so:
\tChorus Stereo (5002315/http://lsp-plug.in/plugins/ladspa/chorus_stereo)
\tChorus Mono (5002314/http://lsp-plug.in/plugins/ladspa/chorus_mono)
"""
        entries = parse_listplugins_output(sample)
        by_name = {e.name: e.is_stereo for e in entries}
        assert by_name["Chorus Stereo"] is True
        assert by_name["Chorus Mono"] is False

    def test_validated_flag_set_from_whitelist(self) -> None:
        # comp_delay_stereo is in VALIDATED_PLUGINS — it should come
        # back with validated=True so the picker can flag it.
        from phonon_stage.dsp.ladspa import parse_listplugins_output

        sample = """\
/usr/lib/ladspa/lsp-plugins-ladspa.so:
\tDelay Compensator (Stereo) (5002274/http://lsp-plug.in/plugins/ladspa/comp_delay_stereo)
\tChorus Stereo (5002315/http://lsp-plug.in/plugins/ladspa/chorus_stereo)
"""
        entries = parse_listplugins_output(sample)
        by_label = {e.label.split("/")[-1]: e.validated for e in entries}
        assert by_label["comp_delay_stereo"] is True
        assert by_label["chorus_stereo"] is False


class TestDspCatalogScan:
    """End-to-end check that GET /dsp/plugins serves the scan result
    when the introspector exposes one, and falls back gracefully."""

    @pytest.mark.asyncio()
    async def test_uses_introspector_scan(
        self,
        client: AsyncClient,
        fake_ladspa_introspector,  # type: ignore[no-untyped-def]
    ) -> None:
        # Seed a catalog directly on the introspector fixture, then
        # reset the module-level cache so the endpoint re-scans.
        import phonon_stage.api.dsp as dsp_mod
        from phonon_stage.dsp.ladspa import CatalogEntry

        dsp_mod._catalog_cache = []
        dsp_mod._catalog_cache_time = 0.0

        fake_ladspa_introspector.register_catalog(
            [
                CatalogEntry(
                    library="lsp-plugins-ladspa",
                    label="http://lsp-plug.in/plugins/ladspa/comp_stereo",
                    name="Compressor Stereo",
                    category="Dynamics",
                    is_stereo=True,
                ),
                CatalogEntry(
                    library="lsp-plugins-ladspa",
                    label="http://lsp-plug.in/plugins/ladspa/art_delay_stereo",
                    name="Artistic Delay Stereo",
                    category="Time",
                    is_stereo=True,
                ),
            ]
        )
        r = await client.get("/dsp/plugins")
        assert r.status_code == 200
        body = r.json()
        names = [e["name"] for e in body]
        assert "Compressor Stereo" in names
        assert "Artistic Delay Stereo" in names
        # Validated entries surface in the response — every entry
        # carries the flag (default False).
        for e in body:
            assert "validated" in e

    @pytest.mark.asyncio()
    async def test_validated_plugins_sort_to_top(
        self,
        client: AsyncClient,
        fake_ladspa_introspector,  # type: ignore[no-untyped-def]
    ) -> None:
        # Pair: one validated, one not. The endpoint sorts validated
        # to the front regardless of category alphabetical order.
        import phonon_stage.api.dsp as dsp_mod
        from phonon_stage.dsp.ladspa import CatalogEntry

        dsp_mod._catalog_cache = []
        dsp_mod._catalog_cache_time = 0.0

        fake_ladspa_introspector.register_catalog(
            [
                # "Chorus Stereo" would normally beat "Delay Compensator"
                # alphabetically AND its category ("Modulation") comes
                # before "Time", so without the validated-first rule
                # chorus would lead.
                CatalogEntry(
                    library="lsp-plugins-ladspa",
                    label="http://lsp-plug.in/plugins/ladspa/chorus_stereo",
                    name="Chorus Stereo",
                    category="Modulation",
                    is_stereo=True,
                    validated=False,
                ),
                CatalogEntry(
                    library="lsp-plugins-ladspa",
                    label="http://lsp-plug.in/plugins/ladspa/comp_delay_stereo",
                    name="Delay Compensator (Stereo)",
                    category="Time",
                    is_stereo=True,
                    validated=True,
                ),
            ]
        )
        r = await client.get("/dsp/plugins")
        assert r.status_code == 200
        body = r.json()
        assert body[0]["validated"] is True
        assert body[0]["label"].endswith("comp_delay_stereo")
