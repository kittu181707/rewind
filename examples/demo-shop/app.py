"""Demo-shop FastAPI application backed by Flagsmith and SQLite."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from flagsmith import Flagsmith
from pydantic import BaseModel
import uvicorn
from urllib3.util.retry import Retry

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from reset import default_db_path, init_db  # noqa: E402


KNOWN_FLAGS = ("checkout_v2", "cart_compat_reader")


class EvalRequest(BaseModel):
    user_id: str
    traits: dict[str, Any] | None = None


class CartAddRequest(BaseModel):
    user_id: str
    sku: str
    name: str
    quantity: int = 1
    price_cents: int = 100


class CheckoutRequest(BaseModel):
    user_id: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    return default_db_path()


def _app_variant() -> str:
    variant = os.environ.get("APP_VARIANT", "default").strip().lower()
    return variant or "default"


def _connect() -> sqlite3.Connection:
    db = _db_path()
    if not db.exists():
        init_db(db)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _create_flagsmith_client() -> Flagsmith:
    env_key = os.environ.get("FLAGSMITH_ENV_KEY", "").strip()
    if not env_key:
        raise RuntimeError("FLAGSMITH_ENV_KEY environment variable is required")
    return Flagsmith(
        environment_key=env_key,
        enable_local_evaluation=False,
        retries=Retry(total=0),
    )


_CLIENT: Flagsmith | None = None
_CLIENT_KEY: str | None = None


def _get_client() -> Flagsmith:
    global _CLIENT, _CLIENT_KEY
    env_key = os.environ.get("FLAGSMITH_ENV_KEY", "").strip()
    if _CLIENT is None or _CLIENT_KEY != env_key:
        _CLIENT = _create_flagsmith_client()
        _CLIENT_KEY = env_key
    return _CLIENT


def _evaluate_flags(user_id: str, traits: dict[str, Any]) -> dict[str, bool]:
    client = _get_client()
    flags_obj = client.get_identity_flags(identifier=user_id, traits=traits)
    effective: dict[str, bool] = {}
    for flag_name in KNOWN_FLAGS:
        try:
            effective[flag_name] = bool(flags_obj.is_feature_enabled(flag_name))
        except Exception:
            effective[flag_name] = False
    try:
        for flag in flags_obj.all_flags():
            effective[flag.feature_name] = bool(flag.enabled)
    except Exception:
        pass
    return effective


def _load_customer_and_cart(conn: sqlite3.Connection, user_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cust_row = conn.execute(
        "SELECT user_id, name, is_vip FROM customers WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if cust_row is None:
        raise HTTPException(status_code=404, detail=f"Unknown user: {user_id}")
    cart_row = conn.execute(
        "SELECT cart_id, user_id, schema_version, payload_json, status FROM carts WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if cart_row is None:
        raise HTTPException(status_code=404, detail=f"No cart for user: {user_id}")
    customer = {
        "user_id": cust_row["user_id"],
        "name": cust_row["name"],
        "traits": {"is_vip": bool(cust_row["is_vip"])},
    }
    cart = {
        "cart_id": cart_row["cart_id"],
        "schema_version": cart_row["schema_version"],
        "payload": json.loads(cart_row["payload_json"]),
        "status": cart_row["status"],
    }
    return customer, cart


def _items_from_v1(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("items", [])
    return [
        {
            "sku": str(item["sku"]),
            "name": str(item["name"]),
            "qty": int(item["qty"]),
            "price_cents": int(item["price_cents"]),
        }
        for item in raw
    ]


def _compat_convert_v2_to_v1(payload: dict[str, Any]) -> list[dict[str, Any]]:
    line_items = payload.get("line_items")
    if not isinstance(line_items, list):
        raise ValueError("Malformed v2 cart payload")
    return [
        {
            "sku": str(entry["product_id"]),
            "name": str(entry["title"]),
            "qty": int(entry["quantity"]),
            "price_cents": int(entry["unit_cents"]),
        }
        for entry in line_items
    ]


def _serialize_v1_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items}


def _serialize_v2_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    total_qty = sum(int(i["qty"]) for i in items)
    subtotal = sum(int(i["qty"]) * int(i["price_cents"]) for i in items)
    return {
        "version": 2,
        "line_items": [
            {
                "product_id": i["sku"],
                "title": i["name"],
                "quantity": int(i["qty"]),
                "unit_cents": int(i["price_cents"]),
            }
            for i in items
        ],
        "summary": {
            "count": total_qty,
            "subtotal_cents": subtotal,
        },
    }


def _summarize(items: list[dict[str, Any]]) -> tuple[int, int]:
    total_items = sum(int(i["qty"]) for i in items)
    total_cents = sum(int(i["qty"]) * int(i["price_cents"]) for i in items)
    return total_items, total_cents


def _read_cart_items(
    schema_version: str,
    payload: dict[str, Any],
    effective_flags: dict[str, bool],
    app_variant: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Return (executed_version, normalized_items) or raise ValueError on incompatible schema."""
    use_v2 = bool(effective_flags.get("checkout_v2", False))
    if use_v2:
        if schema_version == "v2":
            return "v2", _compat_convert_v2_to_v1(payload)
        return "v2", _items_from_v1(payload)

    # Legacy execution path
    if schema_version == "v1":
        return "legacy", _items_from_v1(payload)
    if schema_version == "v2":
        compat_enabled = bool(effective_flags.get("cart_compat_reader", False))
        if app_variant != "no_compat" and compat_enabled:
            return "legacy", _compat_convert_v2_to_v1(payload)
        raise ValueError("Unrecognized cart schema")
    raise ValueError("Unrecognized cart schema")


def _schema_error_response(
    user_id: str,
    cart_id: str,
    effective_flags: dict[str, bool],
) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "detail": "Unrecognized cart schema",
            "error": "Unrecognized cart schema",
            "user_id": user_id,
            "cart_id": cart_id,
            "executed_version": "legacy",
            "effective_flags": effective_flags,
        },
    )


app = FastAPI(title="Rewind Demo Shop")


@app.get("/")
def index() -> Any:
    html_path = _HERE / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return {
        "ok": True,
        "service": "demo-shop",
        "executed_version": "none",
        "effective_flags": {},
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "demo-shop",
        "app_variant": _app_variant(),
        "db_path": str(_db_path()),
        "executed_version": "none",
        "effective_flags": {},
    }


@app.post("/reset")
def reset_endpoint() -> dict[str, Any]:
    res = init_db(_db_path())
    return {
        **res,
        "executed_version": "none",
        "effective_flags": {},
    }


@app.get("/cart/{user_id}")
def get_cart(user_id: str) -> dict[str, Any]:
    conn = _connect()
    try:
        customer, cart = _load_customer_and_cart(conn, user_id)
    finally:
        conn.close()
    items = (
        _compat_convert_v2_to_v1(cart["payload"])
        if cart["schema_version"] == "v2"
        else _items_from_v1(cart["payload"])
    )
    total_items, total_cents = _summarize(items)
    return {
        "ok": True,
        "user_id": customer["user_id"],
        "name": customer["name"],
        "traits": customer["traits"],
        "cart_id": cart["cart_id"],
        "schema_version": cart["schema_version"],
        "items": items,
        "total_items": total_items,
        "total_cents": total_cents,
        "executed_version": "none",
        "effective_flags": {},
    }


@app.post("/eval")
def eval_endpoint(req: EvalRequest) -> dict[str, Any]:
    conn = _connect()
    try:
        customer, cart = _load_customer_and_cart(conn, req.user_id)
    finally:
        conn.close()

    traits = customer["traits"] if req.traits is None else dict(req.traits)
    effective_flags = _evaluate_flags(req.user_id, traits)
    executed_version = "v2" if effective_flags.get("checkout_v2", False) else "legacy"
    return {
        "ok": True,
        "user_id": customer["user_id"],
        "cart_id": cart["cart_id"],
        "traits": customer["traits"],
        "schema_version": cart["schema_version"],
        "executed_version": executed_version,
        "effective_flags": effective_flags,
    }


@app.post("/cart/add")
def cart_add(req: CartAddRequest) -> Any:
    if req.quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be positive")
    conn = _connect()
    try:
        customer, cart = _load_customer_and_cart(conn, req.user_id)
        effective_flags = _evaluate_flags(req.user_id, customer["traits"])
        try:
            executed_version, items = _read_cart_items(
                cart["schema_version"],
                cart["payload"],
                effective_flags,
                _app_variant(),
            )
        except ValueError:
            return _schema_error_response(req.user_id, cart["cart_id"], effective_flags)

        merged = False
        for item in items:
            if item["sku"] == req.sku:
                item["qty"] = int(item["qty"]) + int(req.quantity)
                item["price_cents"] = int(req.price_cents)
                merged = True
                break
        if not merged:
            items.append(
                {
                    "sku": req.sku,
                    "name": req.name,
                    "qty": int(req.quantity),
                    "price_cents": int(req.price_cents),
                }
            )

        new_schema = "v2" if executed_version == "v2" else "v1"
        new_payload = _serialize_v2_payload(items) if new_schema == "v2" else _serialize_v1_payload(items)
        conn.execute(
            """
            UPDATE carts
            SET schema_version = ?, payload_json = ?, status = 'open', updated_at = ?
            WHERE cart_id = ?
            """,
            (new_schema, json.dumps(new_payload), _utc_now(), cart["cart_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    total_items, total_cents = _summarize(items)
    return {
        "ok": True,
        "user_id": customer["user_id"],
        "cart_id": cart["cart_id"],
        "traits": customer["traits"],
        "schema_version": new_schema,
        "items": items,
        "total_items": total_items,
        "total_cents": total_cents,
        "executed_version": executed_version,
        "effective_flags": effective_flags,
    }


@app.post("/checkout")
def checkout(req: CheckoutRequest) -> Any:
    conn = _connect()
    try:
        customer, cart = _load_customer_and_cart(conn, req.user_id)
        effective_flags = _evaluate_flags(req.user_id, customer["traits"])
        try:
            executed_version, items = _read_cart_items(
                cart["schema_version"],
                cart["payload"],
                effective_flags,
                _app_variant(),
            )
        except ValueError:
            return _schema_error_response(req.user_id, cart["cart_id"], effective_flags)

        total_items, total_cents = _summarize(items)
        order_id = f"ord-{uuid.uuid4().hex[:10]}"
        conn.execute(
            """
            INSERT INTO orders (order_id, cart_id, user_id, executed_version, total_items, total_cents, items_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                cart["cart_id"],
                customer["user_id"],
                executed_version,
                total_items,
                total_cents,
                json.dumps(items),
                _utc_now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "order_id": order_id,
        "user_id": customer["user_id"],
        "cart_id": cart["cart_id"],
        "traits": customer["traits"],
        "schema_version": cart["schema_version"],
        "items": items,
        "total_items": total_items,
        "total_cents": total_cents,
        "executed_version": executed_version,
        "effective_flags": effective_flags,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run demo-shop FastAPI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8001")))
    args = parser.parse_args(argv)
    init_db_if_missing = not _db_path().exists()
    if init_db_if_missing:
        init_db(_db_path())
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
