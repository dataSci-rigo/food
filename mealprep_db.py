"""Fridge inventory + log DB."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config


@contextmanager
def _conn():
    con = sqlite3.connect(config.MEALPREP_DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    os.makedirs(os.path.dirname(config.MEALPREP_DB), exist_ok=True)
    with _conn() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS fridge (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name   TEXT    NOT NULL COLLATE NOCASE,
                quantity    REAL    NOT NULL DEFAULT 0,
                unit        TEXT    NOT NULL DEFAULT 'g',
                added_at    TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS fridge_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                action      TEXT    NOT NULL,
                item_name   TEXT    NOT NULL,
                quantity    REAL,
                unit        TEXT,
                logged_at   TEXT    NOT NULL
            )
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_item(name: str, quantity: float, unit: str = "g") -> None:
    now = _now()
    with _conn() as con:
        row = con.execute(
            "SELECT id, quantity FROM fridge WHERE item_name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row:
            con.execute(
                "UPDATE fridge SET quantity = quantity + ?, unit = ?, updated_at = ? WHERE id = ?",
                (quantity, unit, now, row["id"]),
            )
        else:
            con.execute(
                "INSERT INTO fridge (item_name, quantity, unit, added_at, updated_at) VALUES (?,?,?,?,?)",
                (name, quantity, unit, now, now),
            )
        con.execute(
            "INSERT INTO fridge_log (action, item_name, quantity, unit, logged_at) VALUES (?,?,?,?,?)",
            ("add", name, quantity, unit, now),
        )


def remove_item(name: str, quantity: float | None = None) -> bool:
    now = _now()
    with _conn() as con:
        row = con.execute(
            "SELECT id, quantity, unit FROM fridge WHERE item_name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if not row:
            return False
        qty = quantity if quantity is not None else row["quantity"]
        if quantity is None or quantity >= row["quantity"]:
            con.execute("DELETE FROM fridge WHERE id = ?", (row["id"],))
        else:
            con.execute(
                "UPDATE fridge SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
                (quantity, now, row["id"]),
            )
        con.execute(
            "INSERT INTO fridge_log (action, item_name, quantity, unit, logged_at) VALUES (?,?,?,?,?)",
            ("remove", name, qty, row["unit"], now),
        )
    return True


def eat_item(name: str, quantity: float | None = None) -> tuple[bool, float | None, str | None]:
    """Deduct from fridge and return (found, qty_eaten, unit) for nutrition logging."""
    now = _now()
    with _conn() as con:
        row = con.execute(
            "SELECT id, quantity, unit FROM fridge WHERE item_name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if not row:
            return False, None, None
        qty_eaten = quantity if quantity is not None else row["quantity"]
        if quantity is None or quantity >= row["quantity"]:
            con.execute("DELETE FROM fridge WHERE id = ?", (row["id"],))
        else:
            con.execute(
                "UPDATE fridge SET quantity = quantity - ?, updated_at = ? WHERE id = ?",
                (quantity, now, row["id"]),
            )
        con.execute(
            "INSERT INTO fridge_log (action, item_name, quantity, unit, logged_at) VALUES (?,?,?,?,?)",
            ("eat", name, qty_eaten, row["unit"], now),
        )
    return True, qty_eaten, row["unit"]


def get_fridge() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT item_name, quantity, unit, updated_at FROM fridge ORDER BY item_name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_fridge_log(limit: int = 20) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM fridge_log ORDER BY logged_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_quantity(item_id: int, quantity: float) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE fridge SET quantity = ?, updated_at = ? WHERE id = ?",
            (quantity, _now(), item_id),
        )


def delete_item(item_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM fridge WHERE id = ?", (item_id,))
