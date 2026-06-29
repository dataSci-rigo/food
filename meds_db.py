"""Medication / supplement catalog + dose log DB."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config


@contextmanager
def _conn():
    con = sqlite3.connect(config.MEDS_DB, check_same_thread=False)
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
    os.makedirs(os.path.dirname(config.MEDS_DB), exist_ok=True)
    with _conn() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS med_catalog (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    UNIQUE NOT NULL,
                dose_amount REAL,
                dose_unit   TEXT,
                category    TEXT    NOT NULL DEFAULT 'supplement',
                notes       TEXT,
                active      INTEGER NOT NULL DEFAULT 1,
                added_at    TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS dose_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                med_name    TEXT    NOT NULL,
                dose_amount REAL,
                dose_unit   TEXT,
                logged_at   TEXT    NOT NULL,
                notes       TEXT
            )
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def add_med(name: str, dose_amount: float | None, dose_unit: str | None,
            category: str = "supplement", notes: str | None = None) -> int:
    now = _now()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO med_catalog (name, dose_amount, dose_unit, category, notes, added_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                   dose_amount = excluded.dose_amount,
                   dose_unit   = excluded.dose_unit,
                   category    = excluded.category,
                   notes       = excluded.notes,
                   active      = 1""",
            (name, dose_amount, dose_unit, category, notes, now),
        )
        return cur.lastrowid


def remove_med(name: str) -> bool:
    with _conn() as con:
        cur = con.execute(
            "UPDATE med_catalog SET active = 0 WHERE name = ? COLLATE NOCASE", (name,)
        )
        return cur.rowcount > 0


def get_catalog(active_only: bool = True) -> list[dict]:
    with _conn() as con:
        query = "SELECT * FROM med_catalog"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY category, name"
        rows = con.execute(query).fetchall()
    return [dict(r) for r in rows]


def log_dose(med_name: str, dose_amount: float | None = None,
             dose_unit: str | None = None, notes: str | None = None) -> int:
    now = _now()
    # Auto-fill dose from catalog if not provided
    if dose_amount is None:
        with _conn() as con:
            row = con.execute(
                "SELECT dose_amount, dose_unit FROM med_catalog WHERE name = ? COLLATE NOCASE",
                (med_name,),
            ).fetchone()
            if row:
                dose_amount = row["dose_amount"]
                dose_unit   = row["dose_unit"]
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO dose_log (med_name, dose_amount, dose_unit, logged_at, notes) VALUES (?,?,?,?,?)",
            (med_name, dose_amount, dose_unit, now, notes),
        )
        return cur.lastrowid


def get_today_doses() -> list[dict]:
    date = _today()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM dose_log WHERE logged_at LIKE ? ORDER BY logged_at",
            (f"{date}%",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_dose_log(days: int = 7) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM dose_log ORDER BY logged_at DESC LIMIT ?", (days * 20,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_dose(dose_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM dose_log WHERE id = ?", (dose_id,))


def update_med(med_id: int, **kwargs) -> None:
    allowed = {"name", "dose_amount", "dose_unit", "category", "notes", "active"}
    fields  = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as con:
        con.execute(
            f"UPDATE med_catalog SET {set_clause} WHERE id = ?",
            (*fields.values(), med_id),
        )
