"""Reset demo-shop SQLite database to a clean baseline state."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASELINE_CUSTOMERS: list[dict[str, Any]] = [
    {
        "user_id": "alice",
        "name": "Alice Vance",
        "is_vip": True,
        "cart_id": "cart-alice",
        "items": [
            {"sku": "coffee", "name": "House Coffee", "qty": 1, "price_cents": 500},
        ],
    },
    {
        "user_id": "carol",
        "name": "Carol Lin",
        "is_vip": True,
        "cart_id": "cart-carol",
        "items": [
            {"sku": "tea", "name": "Matcha Tea", "qty": 1, "price_cents": 400},
        ],
    },
    {
        "user_id": "bob",
        "name": "Bob Miller",
        "is_vip": False,
        "cart_id": "cart-bob",
        "items": [
            {"sku": "bagel", "name": "Plain Bagel", "qty": 1, "price_cents": 300},
        ],
    },
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_db_path() -> Path:
    env_db = os.environ.get("DB_PATH", "").strip()
    if env_db:
        return Path(env_db)
    return Path(__file__).with_name("demo_shop.db")


def init_db(db_path: str | Path) -> dict[str, Any]:
    """Recreate the SQLite schema and insert clean baseline customers and v1 carts."""
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(f"{target}{suffix}")
        if candidate.exists():
            candidate.unlink()

    now = _utc_now()
    conn = sqlite3.connect(str(target))
    try:
        conn.execute(
            """
            CREATE TABLE customers (
                user_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                is_vip INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE carts (
                cart_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                schema_version TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES customers(user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                cart_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                executed_version TEXT NOT NULL,
                total_items INTEGER NOT NULL,
                total_cents INTEGER NOT NULL,
                items_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        for cust in BASELINE_CUSTOMERS:
            conn.execute(
                "INSERT INTO customers (user_id, name, is_vip, updated_at) VALUES (?, ?, ?, ?)",
                (cust["user_id"], cust["name"], 1 if cust["is_vip"] else 0, now),
            )
            payload = {"items": cust["items"]}
            conn.execute(
                """
                INSERT INTO carts (cart_id, user_id, schema_version, payload_json, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cust["cart_id"], cust["user_id"], "v1", json.dumps(payload), "open", now),
            )

        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "db": str(target),
        "users": [c["user_id"] for c in BASELINE_CUSTOMERS],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reset demo-shop SQLite database.")
    parser.add_argument("--db", default=None, help="Path to SQLite database file.")
    args = parser.parse_args(argv)
    db_path = Path(args.db) if args.db else default_db_path()
    result = init_db(db_path)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
