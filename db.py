from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config


@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
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
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with _conn() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
            CREATE TABLE IF NOT EXISTS food_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT    NOT NULL,
                logged_at   TEXT    NOT NULL,
                user_input  TEXT    NOT NULL,
                food_name   TEXT    NOT NULL,
                source      TEXT    NOT NULL,
                grams_eaten REAL,
                calories    REAL,
                sat_fat_g   REAL,
                sodium_mg   REAL,
                carbs_g     REAL,
                sugar_g     REAL,
                fiber_g     REAL,
                glycemic_index REAL,
                gi_source   TEXT,
                liked       INTEGER
            )
        """)
        # Migrate existing DBs that predate fiber_g column
        try:
            con.execute("ALTER TABLE food_log ADD COLUMN fiber_g REAL")
        except Exception:
            pass
        con.execute("""
            CREATE TABLE IF NOT EXISTS food_history (
                food_name          TEXT UNIQUE NOT NULL,
                use_count          INTEGER     NOT NULL DEFAULT 1,
                last_used          TEXT        NOT NULL,
                per_100g_calories  REAL,
                per_100g_sat_fat   REAL,
                per_100g_sodium    REAL,
                per_100g_carbs     REAL,
                per_100g_sugar     REAL,
                glycemic_index     REAL,
                gi_source          TEXT,
                source             TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS gi_cache (
                key        TEXT UNIQUE NOT NULL,
                gi         REAL        NOT NULL,
                source     TEXT        NOT NULL,
                cached_at  TEXT        NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id      INTEGER PRIMARY KEY,
                state        TEXT    NOT NULL DEFAULT 'idle',
                pending_json TEXT,
                updated_at   TEXT    NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT UNIQUE NOT NULL COLLATE NOCASE,
                user_input       TEXT,
                ingredients_json TEXT,
                total_json       TEXT,
                saved_at         TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS food_catalog (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                name             TEXT UNIQUE NOT NULL COLLATE NOCASE,
                cal_per_100g     REAL,
                sat_fat_per_100g REAL,
                sodium_per_100g  REAL,
                carbs_per_100g   REAL,
                sugar_per_100g   REAL,
                fiber_per_100g   REAL,
                serving_g        REAL,
                source           TEXT,
                saved_at         TEXT NOT NULL
            )
        """)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_food(
    date: str,
    user_input: str,
    food_name: str,
    source: str,
    grams: float | None,
    nutrients: dict,   # keys: calories, sat_fat_g, sodium_mg, carbs_g, sugar_g, fiber_g (scaled)
    gi: float | None,
    gi_source: str | None,
) -> int:
    now = _now_utc()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO food_log
               (date, logged_at, user_input, food_name, source,
                grams_eaten, calories, sat_fat_g, sodium_mg, carbs_g, sugar_g, fiber_g,
                glycemic_index, gi_source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                date, now, user_input, food_name, source,
                grams,
                nutrients.get("calories"),
                nutrients.get("sat_fat_g"),
                nutrients.get("sodium_mg"),
                nutrients.get("carbs_g"),
                nutrients.get("sugar_g"),
                nutrients.get("fiber_g"),
                gi,
                gi_source,
            ),
        )
        row_id = cur.lastrowid
        # Upsert into history (per-100g values stored separately for reuse)
        per = nutrients.get("_per_100g", {})
        con.execute(
            """INSERT INTO food_history
               (food_name, use_count, last_used,
                per_100g_calories, per_100g_sat_fat, per_100g_sodium,
                per_100g_carbs, per_100g_sugar, glycemic_index, gi_source, source)
               VALUES (?,1,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(food_name) DO UPDATE SET
                   use_count = use_count + 1,
                   last_used = excluded.last_used,
                   glycemic_index = COALESCE(excluded.glycemic_index, glycemic_index),
                   gi_source = COALESCE(excluded.gi_source, gi_source)""",
            (
                food_name, now,
                per.get("calories"), per.get("sat_fat_g"), per.get("sodium_mg"),
                per.get("carbs_g"), per.get("sugar_g"),
                gi, gi_source, source,
            ),
        )
    return row_id


def update_liked(log_id: int, liked: bool):
    with _conn() as con:
        con.execute(
            "UPDATE food_log SET liked=? WHERE id=?",
            (1 if liked else 0, log_id),
        )


def delete_log(log_id: int):
    with _conn() as con:
        con.execute("DELETE FROM food_log WHERE id=?", (log_id,))


def get_day_log(date: str) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM food_log WHERE date=? ORDER BY logged_at", (date,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_day_totals(date: str) -> dict:
    with _conn() as con:
        row = con.execute(
            """SELECT
                COALESCE(SUM(calories),0)  AS calories,
                COALESCE(SUM(sat_fat_g),0) AS sat_fat_g,
                COALESCE(SUM(sodium_mg),0) AS sodium_mg,
                COALESCE(SUM(carbs_g),0)   AS carbs_g,
                COALESCE(SUM(sugar_g),0)   AS sugar_g
               FROM food_log WHERE date=?""",
            (date,),
        ).fetchone()
    return dict(row) if row else {k: 0 for k in config.DAILY_LIMITS}


def get_history_totals(days: int = 7) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT date,
                COALESCE(SUM(calories),0)  AS calories,
                COALESCE(SUM(sat_fat_g),0) AS sat_fat_g,
                COALESCE(SUM(sodium_mg),0) AS sodium_mg,
                COALESCE(SUM(carbs_g),0)   AS carbs_g,
                COALESCE(SUM(sugar_g),0)   AS sugar_g
               FROM food_log
               GROUP BY date
               ORDER BY date DESC
               LIMIT ?""",
            (days,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_suggestions(prefix: str, limit: int = 5) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            """SELECT food_name, use_count, per_100g_calories, glycemic_index, source
               FROM food_history
               WHERE food_name LIKE ?
               ORDER BY use_count DESC
               LIMIT ?""",
            (f"{prefix}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- GI cache ----------

def gi_get(key: str) -> tuple[float, str] | None:
    with _conn() as con:
        row = con.execute(
            "SELECT gi, source FROM gi_cache WHERE key=?", (key.lower(),)
        ).fetchone()
    return (row["gi"], row["source"]) if row else None


def gi_set(key: str, gi: float, source: str):
    now = _now_utc()
    with _conn() as con:
        con.execute(
            """INSERT INTO gi_cache (key, gi, source, cached_at) VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET gi=excluded.gi, source=excluded.source,
               cached_at=excluded.cached_at""",
            (key.lower(), gi, source, now),
        )


# ---------- Chat state ----------

def get_state(chat_id: int) -> dict | None:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("pending_json"):
        d["pending"] = json.loads(d["pending_json"])
    return d


def set_state(chat_id: int, state: str, pending: dict | None = None):
    now = _now_utc()
    pending_json = json.dumps(pending) if pending else None
    with _conn() as con:
        con.execute(
            """INSERT INTO chat_state (chat_id, state, pending_json, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                   state=excluded.state,
                   pending_json=excluded.pending_json,
                   updated_at=excluded.updated_at""",
            (chat_id, state, pending_json, now),
        )


def clear_state(chat_id: int):
    set_state(chat_id, "idle", None)


# ---------- Food catalog ----------

# ---------- Recipes ----------

def recipe_save(name: str, user_input: str, ingredients: list[dict], total: dict) -> None:
    import json as _json
    now = _now_utc()
    with _conn() as con:
        con.execute("""
            INSERT INTO recipes (name, user_input, ingredients_json, total_json, saved_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                user_input      = excluded.user_input,
                ingredients_json= excluded.ingredients_json,
                total_json      = excluded.total_json,
                saved_at        = excluded.saved_at
        """, (name, user_input, _json.dumps(ingredients), _json.dumps(total), now))


def recipe_list() -> list[dict]:
    import json as _json
    with _conn() as con:
        rows = con.execute(
            "SELECT id, name, user_input, total_json, saved_at FROM recipes ORDER BY name COLLATE NOCASE"
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        d["total"] = _json.loads(d.pop("total_json", "{}") or "{}")
        results.append(d)
    return results


def recipe_get(name: str) -> dict | None:
    import json as _json
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM recipes WHERE LOWER(name)=?", (name.lower(),)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["ingredients"] = _json.loads(d.pop("ingredients_json", "[]") or "[]")
    d["total"]       = _json.loads(d.pop("total_json", "{}") or "{}")
    return d


def recipe_delete(name: str) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM recipes WHERE LOWER(name)=?", (name.lower(),)
        )
    return cur.rowcount > 0


def catalog_save(name: str, per_100g: dict, serving_g: float | None, source: str) -> None:
    now = _now_utc()
    with _conn() as con:
        con.execute("""
            INSERT INTO food_catalog
                (name, cal_per_100g, sat_fat_per_100g, sodium_per_100g,
                 carbs_per_100g, sugar_per_100g, fiber_per_100g, serving_g, source, saved_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                cal_per_100g     = excluded.cal_per_100g,
                sat_fat_per_100g = excluded.sat_fat_per_100g,
                sodium_per_100g  = excluded.sodium_per_100g,
                carbs_per_100g   = excluded.carbs_per_100g,
                sugar_per_100g   = excluded.sugar_per_100g,
                fiber_per_100g   = excluded.fiber_per_100g,
                serving_g        = COALESCE(excluded.serving_g, serving_g),
                source           = excluded.source,
                saved_at         = excluded.saved_at
        """, (
            name,
            per_100g.get("calories"), per_100g.get("sat_fat_g"),
            per_100g.get("sodium_mg"), per_100g.get("carbs_g"),
            per_100g.get("sugar_g"), per_100g.get("fiber_g"),
            serving_g, source, now,
        ))


def catalog_search(query: str) -> dict | None:
    """Return best matching catalog entry for query, or None."""
    q = query.strip().lower()
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM food_catalog ORDER BY LENGTH(name)"
        ).fetchall()
    entries = [dict(r) for r in rows]
    for e in entries:
        if e["name"].lower() == q:
            return e
    for e in entries:
        if e["name"].lower() in q:
            return e
    for e in entries:
        if q in e["name"].lower():
            return e
    return None


def catalog_list() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM food_catalog ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


def catalog_delete(name: str) -> bool:
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM food_catalog WHERE LOWER(name)=?", (name.lower(),)
        )
    return cur.rowcount > 0


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {config.DB_PATH}")
