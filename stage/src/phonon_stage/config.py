"""Stage configuration loaded from YAML with Pydantic validation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator


class StageConfig(BaseModel):
    """Stage Agent configuration. Loaded from /etc/phonon/stage.yaml."""

    model_config = ConfigDict(extra="forbid")

    bind_address: str = "127.0.0.1"
    port: int = 8401
    log_level: str = "INFO"
    mode: str = "STANDALONE"  # Hardcoded for Étape 1
    machine_id_path: Path = Path("/etc/machine-id")
    standalone_conf_path: Path = Path("/var/lib/phonon/standalone.conf.json")

    @field_validator("bind_address")
    @classmethod
    def reject_wildcard(cls, v: str) -> str:
        if v in ("0.0.0.0", "::"):
            msg = "Binding to 0.0.0.0 or :: is forbidden (SECURITY.md §3.4)"
            raise ValueError(msg)
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            msg = f"log_level must be one of {allowed}, got {v!r}"
            raise ValueError(msg)
        return upper

    @property
    def stage_id(self) -> str:
        content = self.machine_id_path.read_text().strip()
        digest = hashlib.sha256(content.encode()).hexdigest()[:8]
        return f"stage-{digest}"


def load_config(path: Path | None = None) -> StageConfig:
    """Load config from YAML file. Falls back to defaults if file is absent."""
    if path is None or not path.exists():
        return StageConfig()

    raw = path.read_text()
    data: dict[str, Any] = yaml.safe_load(raw) or {}
    return StageConfig(**data)
