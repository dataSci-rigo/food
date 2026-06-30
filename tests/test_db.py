"""Unit tests for db.py — all run against a fresh in-memory-style temp DB."""
from __future__ import annotations

import sqlite3
import pytest
import config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tables(db_path: str) -> set[str]:
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    con.close()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# A1 — Schema
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(tmp_db):
    expected = {"food_log", "food_history", "gi_cache", "chat_state", "recipes", "food_catalog"}
    assert expected.issubset(_tables(tmp_db))


# ---------------------------------------------------------------------------
# A2 — food_log
# ---------------------------------------------------------------------------

def test_log_food_returns_id(tmp_db):
    import db
    row_id = db.log_food(
        date="2026-01-01",
        user_input="banana",
        food_name="Bananas, raw",
        source="usda",
        grams=100.0,
        nutrients={"calories": 89, "sat_fat_g": 0.1, "sodium_mg": 1, "carbs_g": 23, "sugar_g": 12, "fiber_g": 2.6},
        gi=51.0,
        gi_source="test",
    )
    assert isinstance(row_id, int)
    assert row_id > 0


def test_get_day_log_filters_by_date(tmp_db):
    import db
    db.log_food("2026-01-01", "apple", "Apple", "usda", 100, {"calories": 52}, None, None)
    db.log_food("2026-01-02", "egg",   "Egg",   "usda", 50,  {"calories": 78}, None, None)
    rows = db.get_day_log("2026-01-01")
    assert len(rows) == 1
    assert rows[0]["food_name"] == "Apple"


def test_get_day_totals_sums(tmp_db):
    import db
    db.log_food("2026-06-01", "a", "FoodA", "usda", 100,
                {"calories": 200, "sat_fat_g": 2, "sodium_mg": 100, "carbs_g": 30, "sugar_g": 5}, None, None)
    db.log_food("2026-06-01", "b", "FoodB", "usda", 100,
                {"calories": 300, "sat_fat_g": 3, "sodium_mg": 150, "carbs_g": 40, "sugar_g": 8}, None, None)
    totals = db.get_day_totals("2026-06-01")
    assert totals["calories"] == pytest.approx(500, abs=1)
    assert totals["sat_fat_g"] == pytest.approx(5, abs=0.1)
    assert totals["sodium_mg"] == pytest.approx(250, abs=1)


# ---------------------------------------------------------------------------
# A3 — food_catalog
# ---------------------------------------------------------------------------

def test_catalog_save_upsert(tmp_db):
    import db
    per100 = {"calories": 400, "sat_fat_g": 5, "sodium_mg": 200, "carbs_g": 50, "sugar_g": 10, "fiber_g": 3}
    db.catalog_save("Peanut Butter", per100, 32.0, "label")
    db.catalog_save("Peanut Butter", {**per100, "calories": 450}, 32.0, "label")  # update
    entries = db.catalog_list()
    pb = [e for e in entries if e["name"] == "Peanut Butter"]
    assert len(pb) == 1
    assert pb[0]["cal_per_100g"] == pytest.approx(450)


def test_catalog_search_exact(tmp_db):
    import db
    per100 = {"calories": 52, "sat_fat_g": 0, "sodium_mg": 1, "carbs_g": 14, "sugar_g": 10, "fiber_g": 2}
    db.catalog_save("Apple", per100, None, "usda")
    result = db.catalog_search("Apple")
    assert result is not None
    assert result["name"] == "Apple"


def test_catalog_search_partial_name_in_query(tmp_db):
    import db
    per100 = {"calories": 588, "sat_fat_g": 10, "sodium_mg": 10, "carbs_g": 20, "sugar_g": 4, "fiber_g": 2}
    db.catalog_save("peanut butter", per100, 32.0, "label")
    result = db.catalog_search("2 tbsp peanut butter")
    assert result is not None


def test_catalog_search_none(tmp_db):
    import db
    result = db.catalog_search("xyzzy unknown food")
    assert result is None


def test_catalog_delete_returns_true(tmp_db):
    import db
    per100 = {"calories": 100, "sat_fat_g": 0, "sodium_mg": 0, "carbs_g": 0, "sugar_g": 0, "fiber_g": 0}
    db.catalog_save("DeleteMe", per100, None, "test")
    assert db.catalog_delete("DeleteMe") is True


def test_catalog_delete_missing_returns_false(tmp_db):
    import db
    assert db.catalog_delete("DoesNotExist") is False


# ---------------------------------------------------------------------------
# A4 — recipes
# ---------------------------------------------------------------------------

def test_recipe_save_get_roundtrip(tmp_db):
    import db
    ingredients = [
        {"name": "2 large eggs", "grams": 120, "calories": 180},
        {"name": "2 slices bread", "grams": 60, "calories": 160},
    ]
    total = {"calories": 340, "carbs_g": 28, "sat_fat_g": 4, "sodium_mg": 380, "sugar_g": 4, "fiber_g": 2}
    db.recipe_save("Eggs on Toast", "fried eggs with toast", ingredients, total)
    recipe = db.recipe_get("Eggs on Toast")
    assert recipe is not None
    assert recipe["name"] == "Eggs on Toast"
    assert len(recipe["ingredients"]) == 2
    assert recipe["total"]["calories"] == pytest.approx(340)


def test_recipe_list_ordering(tmp_db):
    import db
    empty = {"calories": 0, "carbs_g": 0, "sat_fat_g": 0, "sodium_mg": 0, "sugar_g": 0, "fiber_g": 0}
    db.recipe_save("Zucchini Pasta", "", [], empty)
    db.recipe_save("apple salad",    "", [], empty)
    db.recipe_save("Banana Smoothie","", [], empty)
    names = [r["name"] for r in db.recipe_list()]
    assert names == sorted(names, key=str.lower)


def test_recipe_delete(tmp_db):
    import db
    total = {"calories": 0, "carbs_g": 0, "sat_fat_g": 0, "sodium_mg": 0, "sugar_g": 0, "fiber_g": 0}
    db.recipe_save("TempRecipe", "", [], total)
    assert db.recipe_delete("TempRecipe") is True
    assert db.recipe_get("TempRecipe") is None


# ---------------------------------------------------------------------------
# A5 — chat_state
# ---------------------------------------------------------------------------

def test_set_get_clear_state(tmp_db):
    import db
    chat_id = 123456789
    db.set_state(chat_id, "awaiting_grams", {"foods": ["banana"], "idx": 0})
    row = db.get_state(chat_id)
    assert row["state"] == "awaiting_grams"
    assert row["pending"]["foods"] == ["banana"]
    db.clear_state(chat_id)
    row = db.get_state(chat_id)
    assert row["state"] == "idle"
    assert row.get("pending") is None  # key absent when no pending data


# ---------------------------------------------------------------------------
# A6 — GI cache
# ---------------------------------------------------------------------------

def test_gi_set_get(tmp_db):
    import db
    db.gi_set("BANANA", 51.0, "test")
    result = db.gi_get("banana")   # lowercase lookup
    assert result is not None
    gi, source = result
    assert gi == pytest.approx(51.0)
    assert source == "test"
