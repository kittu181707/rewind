# Connect your own app

Rewind tests a rollback against your own Flagsmith project, your own staging app instances, and a trusted journey command. Start from the [demo config](../examples/demo-shop/rewind.config.yaml) and the [working journey example](../examples/demo-shop/journey.py).

## 1. Config fields (`rewind.config.yaml`)

Never place API keys, SDK keys, or credentials inside `rewind.config.yaml`. Reference uppercase environment variable names only.

- **`project`**: Short identifier used for state isolation and report metadata.
- **`flagsmith.rehearsal`** and **`flagsmith.target`**: Distinct objects with `environment` (Flagsmith environment name) and `key_env` (uppercase env var holding that environment's Flagsmith SDK key).
- **`app`**:
  - `start`: Command template started by Rewind (`--port {port}` or `{port}`). Rewind injects `FLAGSMITH_ENV_KEY` and a fresh `DB_PATH` per instance.
  - `reset`: Command template (`--db {db}` or `{db}`) that restores a clean fixture database between attempts.
  - `journey`: Trusted command template (`--base-url {base_url}` and `--phase {phase}`).
  - `rehearsal_port` and `target_port`: Distinct loopback ports.
- **`source_paths`**: Safe relative paths inside the repository that the agent is allowed to read via `read_source`.
- **`rollback`**: Target state definition with `flag`, `to` (`on` or `off`), and plain-English `goal`.
- **`users`**: Test identities covering both `affected` users and at least one `control` user (`id`, `traits`, `role`).
- **`allowed`**: Strict allowlist governing `flags`, `scopes` (`environment_default`, `segment:<name>`, `identity:<id>`), and `operations` (`set_on`, `set_off`, `delete_override`).
- **`agent`** *(optional)*: Provider/model config (`provider`, `model`, `key_env`, `max_attempts`, optional `fallback`).

## 2. App requirements

- Start on loopback (`127.0.0.1`) using the port provided on startup and read `FLAGSMITH_ENV_KEY` and `DB_PATH` from environment variables.
- Expose a ready `GET /health` endpoint returning HTTP `200` once listening.
- Use remote Flagsmith evaluation with no local cache (`enable_local_evaluation=False`) so flag writes show up as soon as edge workers converge.
- Return `executed_version` and `effective_flags` in every action and evaluation response (`/eval`, `/cart/add`, `/checkout`, and equivalent endpoints).
- Provide a deterministic `reset` command that restores a clean fixture database before each attempt without mutating production or user traits.

## 3. Journey JSON contract

Your journey command (`prepare` and `verify`) is language-agnostic. It prints **one JSON object** to stdout and exits `0` (pass) or `1` (fail):

```json
{
  "ok": false,
  "steps": [
    {
      "name": "checkout",
      "user": "alice",
      "http_status": 500,
      "executed_version": "legacy",
      "effective_flags": {
        "checkout_v2": false,
        "cart_compat_reader": false
      },
      "assertions": [
        {
          "name": "latest_quantities_survive",
          "pass": false,
          "detail": "status=500, error=Unrecognized cart schema"
        }
      ]
    }
  ]
}
```

Requirements verified by both the journey command and `rewind/verifier.py`:

1. Before running action assertions, poll `/eval` until **3 consecutive matching reads** are observed for every configured user (timeout 30 s) with explicit Boolean values in `effective_flags`.
2. Assertions must check the `effective_flags` and `executed_version` returned in the same HTTP response as the cart/checkout action, not only `/eval`.
3. In `prepare`, affected users exercise the new path (`v2`) and persist their latest state while the control user keeps their pre-rollout baseline state.
4. In `verify`, every configured user (both `affected` and `control`) must have a `checkout` step with HTTP `200`, matching `executed_version`, preserved identifiers/totals, and unchanged user traits.
5. Rewind automatically appends `--config` and separate per-environment `--state-file` arguments to the journey command. See [examples/demo-shop/journey.py](../examples/demo-shop/journey.py) for the reference implementation.
