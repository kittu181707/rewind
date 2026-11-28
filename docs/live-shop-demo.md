# Live Demo Shop Beside a Staging Run

You can leave a second `examples/demo-shop/app.py` storefront (which makes no Flagsmith changes) open on port `8003` against `demo-target` while running `rewind ui --config examples/demo-shop/rewind.config.yaml` (which uses ports `8001` for rehearsal and `8002` for its staging fixture).

## Setup

Use a separate disposable SQLite database (`/tmp/live-shop-8003.db`) so the `8003` storefront never collides with Rewind's rehearsal (`8001`), staging (`8002`), drift snapshots, or approvals:

```bash
python examples/demo-shop/reset.py --db /tmp/live-shop-8003.db
DB_PATH=/tmp/live-shop-8003.db FLAGSMITH_ENV_KEY="$FLAGSMITH_ENV_KEY_TARGET" \
  python examples/demo-shop/app.py --port 8003
```

Open `http://127.0.0.1:8003/` alongside Rewind (`alice` selected).

## Live Sequence

1. Approve **Gate 1** in Rewind and wait for **Gate 2**, then approve **Gate 2**.
2. As soon as Rewind's timeline enters **Target: prepare** (`checkout_v2` is `ON`), click **+ Add to cart** on `alice` in the `8003` store. Her cart saves in schema `v2` (`Executed version: v2`).
3. As soon as Rewind enters **Target: verify before recovery** (`checkout_v2` has just been switched `OFF`), click **Checkout** in the `8003` store. The storefront displays **HTTP 500 Error: Unrecognized cart schema** on `legacy`.
4. Wait a few seconds until Rewind finishes **Target: verify after recovery** (`cart_compat_reader` switched `ON`), then click **Checkout** again. The storefront displays **Order placed (legacy, HTTP 200)**.

## Measured Timing (measured once)

- During a scripted live Case A Gate 2 replay (`port 8003` open in Chromium alongside `rewind ui`), the broken HTTP 500 window lasted **10.66 s** (19 consecutive HTTP 500 checkout clicks recorded before switching back to HTTP 200 `legacy`).
- Watching **Target: verify before recovery** in the timeline lets you trigger HTTP 500 in the browser during that 10.66 s window, then click **Checkout** again after **Target: verify after recovery** completes.
- Port `8003` only performs remote Flagsmith SDK evaluation on its own database and does not alter Rewind's ports, Flagsmith state, drift snapshots, or approvals.
