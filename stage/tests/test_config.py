"""Tests for StageConfig — defaults, validation, stage_id generation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from phonon_stage.config import StageConfig, load_config

from .conftest import EXPECTED_STAGE_ID_SUFFIX, KNOWN_MACHINE_ID


class TestStageConfigDefaults:
    def test_default_bind_address(self) -> None:
        cfg = StageConfig(machine_id_path=Path("/dev/null"))
        assert cfg.bind_address == "127.0.0.1"

    def test_default_port(self) -> None:
        cfg = StageConfig(machine_id_path=Path("/dev/null"))
        assert cfg.port == 8401

    def test_default_mode_is_standalone(self) -> None:
        cfg = StageConfig(machine_id_path=Path("/dev/null"))
        assert cfg.mode == "STANDALONE"


class TestStageConfigValidation:
    def test_reject_wildcard_ipv4(self) -> None:
        with pytest.raises(ValidationError, match=r"0\.0\.0\.0"):
            StageConfig(bind_address="0.0.0.0", machine_id_path=Path("/dev/null"))

    def test_reject_wildcard_ipv6(self) -> None:
        with pytest.raises(ValidationError, match=r"::"):
            StageConfig(bind_address="::", machine_id_path=Path("/dev/null"))

    def test_reject_extra_fields(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            StageConfig(
                bind_address="127.0.0.1",
                machine_id_path=Path("/dev/null"),
                unknown_field="oops",  # type: ignore[call-arg]
            )


class TestStageId:
    def test_stage_id_deterministic(self, machine_id_file: Path) -> None:
        cfg = StageConfig(machine_id_path=machine_id_file)
        assert cfg.stage_id == f"stage-{EXPECTED_STAGE_ID_SUFFIX}"

    def test_stage_id_stable_across_calls(self, machine_id_file: Path) -> None:
        cfg = StageConfig(machine_id_path=machine_id_file)
        assert cfg.stage_id == cfg.stage_id


class TestLoadConfig:
    def test_load_missing_file_returns_defaults(self) -> None:
        cfg = load_config(Path("/nonexistent/stage.yaml"))
        assert cfg.bind_address == "127.0.0.1"

    def test_load_yaml_file(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "stage.yaml"
        yaml_file.write_text("bind_address: '10.100.0.50'\nport: 9999\n")
        mid = tmp_path / "machine-id"
        mid.write_text(KNOWN_MACHINE_ID + "\n")

        cfg = load_config(yaml_file)
        assert cfg.bind_address == "10.100.0.50"
        assert cfg.port == 9999
