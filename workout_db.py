"""Workout / exercise log DB."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config


@contextmanager
def _conn():
    con = sqlite3.connect(config.WORKOUT_DB, check_same_thread=False)
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
    os.makedirs(os.path.dirname(config.WORKOUT_DB), exist_ok=True)
    with _conn() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS workout_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                date         TEXT    NOT NULL,
                logged_at    TEXT    NOT NULL,
                exercise     TEXT    NOT NULL,
                sets         INTEGER,
                reps         INTEGER,
                weight_kg    REAL,
                duration_min REAL,
                distance_km  REAL,
                notes        TEXT,
                raw_input    TEXT
            )
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def log_exercise(
    exercise: str,
    sets: int | None = None,
    reps: int | None = None,
    weight_kg: float | None = None,
    duration_min: float | None = None,
    distance_km: float | None = None,
    notes: str | None = None,
    raw_input: str | None = None,
    date: str | None = None,
) -> int:
    now = _now()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO workout_log
               (date, logged_at, exercise, sets, reps, weight_kg,
                duration_min, distance_km, notes, raw_input)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (date or _today(), now, exercise, sets, reps, weight_kg,
             duration_min, distance_km, notes, raw_input),
        )
        return cur.lastrowid


def get_day_log(date: str | None = None) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM workout_log WHERE date = ? ORDER BY logged_at",
            (date or _today(),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_history(days: int = 7) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT date, COUNT(*) AS exercises
               FROM workout_log GROUP BY date
               ORDER BY date DESC LIMIT ?""",
            (days,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_exercise(exercise_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM workout_log WHERE id = ?", (exercise_id,))
