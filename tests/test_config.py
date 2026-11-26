"""Unit tests for rewind.config."""

from pathlib import Path
import pytest

from rewind.config import ConfigError, load_config


def _valid_yaml() -> str:
    return """
project: demo-shop
flagsmith:
  rehearsal: { environment: rehearsal, key_env: FLAGSMITH_ENV_KEY_REHEARSAL }
  target:    { environment: demo-target, key_env: FLAGSMITH_ENV_KEY_TARGET }
app:
  start: "python app.py --port {port}"
  reset: "python reset.py --db {db}"
  journey: "python journey.py --base-url {base_url} --phase {phase}"
  rehearsal_port: 8001
  target_port: 8002
source_paths: ["examples/demo-shop/app.py"]
rollback:
  flag: checkout_v2
  to: off
  goal: "every user executes the legacy checkout and keeps their latest cart"
users:
  - { id: alice, traits: { is_vip: true }, role: affected }
  - { id: carol, traits: { is_vip: true }, role: affected }
  - { id: bob, traits: { is_vip: false }, role: control }
allowed:
  flags: [checkout_v2, cart_compat_reader]
  scopes: [environment_default, "segment:vip_users"]
  operations: [set_on, set_off, delete_override]
agent:
  provider: groq
  model: openai/gpt-oss-120b
  key_env: GROQ_API_KEY
  max_attempts: 3
  fallback: { provider: gemini, model: gemini-3.6-flash, key_env: GEMINI_API_KEY }
"""


def test_load_valid_config(tmp_path: Path):
    p = tmp_path / "rewind.config.yaml"
    p.write_text(_valid_yaml(), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.project == "demo-shop"
    assert cfg.allowed_environments() == {"rehearsal", "demo-target"}
    assert cfg.config_dir == tmp_path.resolve()


@pytest.mark.parametrize(
    "bad_snippet",
    [
        "gsk_1234567890abcdef",
        "AIzaSyDummySecretValue12345",
        "sk-proj-1234567890abcdef",
        "ser.1234567890abcdef",
        "AQ.Ab8RN6KXeTfWZAKT",
    ],
)
def test_rejects_secret_like_values(tmp_path: Path, bad_snippet: str):
    p = tmp_path / "rewind.config.yaml"
    p.write_text(_valid_yaml().replace("GROQ_API_KEY", bad_snippet), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_rejects_bad_scope_and_path_traversal(tmp_path: Path):
    p = tmp_path / "rewind.config.yaml"
    p.write_text(_valid_yaml().replace("segment:vip_users", "bad_scope"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)

    p.write_text(_valid_yaml().replace("examples/demo-shop/app.py", "../secret.py"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)


def test_rejects_same_ports_or_envs(tmp_path: Path):
    p = tmp_path / "rewind.config.yaml"
    p.write_text(_valid_yaml().replace("target_port: 8002", "target_port: 8001"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)

    p.write_text(_valid_yaml().replace("environment: demo-target", "environment: rehearsal"), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p)
