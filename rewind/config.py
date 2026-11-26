"""Configuration loader and validator for rewind.config.yaml."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SECRET_PREFIX_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{8,}|gsk_[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{8,}|"
    r"AQ\.[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|"
    r"ser\.[A-Za-z0-9]{10,}|Bearer\s+[A-Za-z0-9._-]{8,})"
)
SCOPE_RE = re.compile(r"^(?:environment_default|segment:[A-Za-z0-9_.-]+|identity:[A-Za-z0-9_.-]+)$")
ALLOWED_OPS = ("set_on", "set_off", "delete_override")


class ConfigError(ValueError):
    """Raised when rewind.config.yaml fails schema or safety validation."""


def _reject_secret_like(value: Any, field_name: str) -> None:
    if isinstance(value, str):
        if SECRET_PREFIX_RE.search(value):
            raise ConfigError(f"{field_name} appears to contain a secret value; reference an env var name instead")
        if "=" in value and any(tok in value.upper() for tok in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            raise ConfigError(f"{field_name} appears to contain an inline credential assignment")
    elif isinstance(value, dict):
        for k, v in value.items():
            _reject_secret_like(v, f"{field_name}.{k}")
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _reject_secret_like(item, f"{field_name}[{idx}]")


class FlagsmithEnvConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str = Field(min_length=1)
    key_env: str = Field(min_length=1)

    @field_validator("key_env")
    @classmethod
    def check_key_env_name(cls, v: str) -> str:
        if not ENV_VAR_NAME_RE.match(v):
            raise ValueError(f"key_env must be an uppercase env var name, got {v!r}")
        return v


class FlagsmithConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rehearsal: FlagsmithEnvConfig
    target: FlagsmithEnvConfig

    @model_validator(mode="after")
    def distinct_environments(self) -> "FlagsmithConfig":
        if self.rehearsal.environment == self.target.environment:
            raise ValueError("rehearsal and target environments must be distinct")
        if self.rehearsal.key_env == self.target.key_env:
            raise ValueError("rehearsal and target key_env must be distinct")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str = Field(min_length=1)
    reset: str = Field(min_length=1)
    journey: str = Field(min_length=1)
    rehearsal_port: int = Field(ge=1024, le=65535)
    target_port: int = Field(ge=1024, le=65535)

    @model_validator(mode="after")
    def distinct_ports(self) -> "AppConfig":
        if self.rehearsal_port == self.target_port:
            raise ValueError("rehearsal_port and target_port must be distinct")
        return self


class RollbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flag: str = Field(min_length=1)
    to: Literal["on", "off"]
    goal: str = Field(min_length=1)

    @field_validator("to", mode="before")
    @classmethod
    def normalize_yaml_bool(cls, v: Any) -> Any:
        if v is False:
            return "off"
        if v is True:
            return "on"
        if isinstance(v, str):
            return v.strip().lower()
        return v


class UserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    traits: dict[str, Any] = Field(default_factory=dict)
    role: Literal["affected", "control"]


class AllowedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flags: list[str] = Field(min_length=1)
    scopes: list[str] = Field(min_length=1)
    operations: list[Literal["set_on", "set_off", "delete_override"]] = Field(min_length=1)

    @field_validator("scopes")
    @classmethod
    def check_scopes(cls, scopes: list[str]) -> list[str]:
        for scope in scopes:
            if not SCOPE_RE.match(scope):
                raise ValueError(f"Invalid scope {scope!r}; expected environment_default, segment:<name>, or identity:<id>")
        return scopes


class FallbackAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    key_env: str = Field(min_length=1)

    @field_validator("key_env")
    @classmethod
    def check_key_env_name(cls, v: str) -> str:
        if not ENV_VAR_NAME_RE.match(v):
            raise ValueError(f"key_env must be an uppercase env var name, got {v!r}")
        return v


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    key_env: str = Field(min_length=1)
    max_attempts: int = Field(ge=1, le=5)
    fallback: FallbackAgentConfig | None = None

    @field_validator("key_env")
    @classmethod
    def check_key_env_name(cls, v: str) -> str:
        if not ENV_VAR_NAME_RE.match(v):
            raise ValueError(f"key_env must be an uppercase env var name, got {v!r}")
        return v


class RewindConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str = Field(min_length=1)
    flagsmith: FlagsmithConfig
    app: AppConfig
    source_paths: list[str] = Field(min_length=1)
    rollback: RollbackConfig
    users: list[UserConfig] = Field(min_length=1)
    allowed: AllowedConfig
    agent: AgentConfig | None = None
    config_dir: Path | None = Field(default=None, exclude=True)
    config_path: Path | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def check_semantics(self) -> "RewindConfig":
        roles = {u.role for u in self.users}
        if "affected" not in roles or "control" not in roles:
            raise ValueError("users must include at least one 'affected' and one 'control' user")
        ids = [u.id for u in self.users]
        if len(ids) != len(set(ids)):
            raise ValueError("user ids must be unique")
        if self.rollback.flag not in self.allowed.flags:
            raise ValueError("rollback.flag must appear in allowed.flags")
        for sp in self.source_paths:
            p = Path(sp)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError(f"source_paths entries must be safe relative paths, got {sp!r}")
        return self

    def allowed_environments(self) -> set[str]:
        return {self.flagsmith.rehearsal.environment, self.flagsmith.target.environment}

    def env_key_for(self, environment: str) -> str:
        for cfg in (self.flagsmith.rehearsal, self.flagsmith.target):
            if cfg.environment == environment:
                val = os.getenv(cfg.key_env, "").strip()
                if not val:
                    raise ConfigError(f"Environment variable {cfg.key_env} for {environment!r} is not set")
                return val
        raise ConfigError(f"Environment {environment!r} is not in allowlist")


def load_config(path: str | Path) -> RewindConfig:
    """Load and validate a rewind.config.yaml file."""
    cfg_path = Path(path).resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"Config file not found: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping in {cfg_path}")

    _reject_secret_like(raw, "config")
    try:
        cfg = RewindConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"Invalid config in {cfg_path}: {exc}") from exc
    cfg.config_dir = cfg_path.parent
    cfg.config_path = cfg_path
    return cfg
