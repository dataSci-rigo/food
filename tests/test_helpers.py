"""Unit tests for pure helper functions in nutrition_handlers.py."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
import pytest

# nutrition_handlers imports aiogram at module level; it's available in p312.
# We still need config patched before importing so DB_PATH doesn't matter.
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from nutrition_handlers import (
    _lookup_result_to_per100g,
    _scale_nutrients,
    _ingredient_text,
    _breakdown_summary_text,
    _progress_bar,
)
from nutrition import FoodResult


# ---------------------------------------------------------------------------
# C1 — _lookup_result_to_per100g()
# ---------------------------------------------------------------------------

def test_per100g_basis_returned_as_is():
    r = FoodResult(
        name="banana", source="usda", basis="per_100g",
        calories=89, saturated_fat_g=0.1, sodium_mg=1, carbs_g=23, sugar_g=12, fiber_g=2.6,
    )
    out = _lookup_result_to_per100g(r)
    assert out is not None
    assert out["calories"] == pytest.approx(89)
    assert out["carbs_g"] == pytest.approx(23)


def test_per_serving_normalized_to_100g():
    r = FoodResult(
        name="Test Bar", source="apininjas", basis="per_serving",
        calories=200, saturated_fat_g=4, sodium_mg=100, carbs_g=25, sugar_g=10, fiber_g=2,
        serving_g=50,
    )
    out = _lookup_result_to_per100g(r)
    assert out is not None
    # 200 kcal / 50g * 100g = 400 kcal/100g
    assert out["calories"] == pytest.approx(400)
    assert out["carbs_g"] == pytest.approx(50)


def test_per_serving_without_serving_g_returns_none():
    r = FoodResult(
        name="Mystery Food", source="apininjas", basis="per_serving",
        calories=150, serving_g=None,
    )
    assert _lookup_result_to_per100g(r) is None


def test_none_input_returns_none():
    assert _lookup_result_to_per100g(None) is None


# ---------------------------------------------------------------------------
# C2 — _scale_nutrients()
# ---------------------------------------------------------------------------

def test_scale_nutrients_150g():
    per100 = {"calories": 200, "carbs_g": 30, "sat_fat_g": 5, "sodium_mg": 100, "sugar_g": 8, "fiber_g": 3}
    out = _scale_nutrients(per100, 150)
    assert out["calories"] == pytest.approx(300)
    assert out["carbs_g"] == pytest.approx(45)
    assert out["sat_fat_g"] == pytest.approx(7.5)


def test_scale_nutrients_handles_none_values():
    per100 = {"calories": None, "carbs_g": 20, "sat_fat_g": None, "sodium_mg": 50, "sugar_g": 5, "fiber_g": 1}
    out = _scale_nutrients(per100, 100)
    assert out["calories"] == pytest.approx(0)   # None treated as 0
    assert out["carbs_g"] == pytest.approx(20)


# ---------------------------------------------------------------------------
# C3 — _ingredient_text()
# ---------------------------------------------------------------------------

def test_ingredient_text_with_lookup():
    ing = {
        "name": "2 large eggs fried",
        "final_grams": 120,
        "claude_grams": 120,
        "claude_calories": 180,
        "claude_carbs_g": 1,
        "claude_sat_fat_g": 5,
        "lookup_found": True,
        "lookup_source": "usda",
        "lookup_per_100g": {"calories": 150, "carbs_g": 0.8, "sat_fat_g": 4.2,
                            "sodium_mg": 120, "sugar_g": 0, "fiber_g": 0},
    }
    text = _ingredient_text(ing, 0, 3)
    assert "Ingredient 1/3" in text
    assert "2 large eggs fried" in text
    assert "Our DB" in text
    assert "AI guess" in text


def test_ingredient_text_lookup_not_found():
    ing = {
        "name": "mystery spice blend",
        "final_grams": 5,
        "claude_calories": 15,
        "claude_carbs_g": 2,
        "claude_sat_fat_g": 0,
        "lookup_found": False,
    }
    text = _ingredient_text(ing, 2, 4)
    assert "not found" in text
    assert "Ingredient 3/4" in text


# ---------------------------------------------------------------------------
# C4 — _breakdown_summary_text()
# ---------------------------------------------------------------------------

def test_breakdown_summary_uses_lookup_when_found():
    pending = {
        "ingredients": [
            {
                "name": "eggs",
                "status": "agreed",
                "final_grams": 120,
                "lookup_found": True,
                "lookup_per_100g": {"calories": 150, "carbs_g": 1, "sat_fat_g": 4,
                                    "sodium_mg": 100, "sugar_g": 0, "fiber_g": 0},
                "claude_calories": 180,
            },
        ],
        "ai_estimate": {"calories": 200, "assumptions": "standard portions"},
        "date": "2026-01-01",
    }
    # Patch db.get_day_totals so we don't need a real DB
    with patch("nutrition_handlers.db.get_day_totals", return_value={
        "calories": 0, "sat_fat_g": 0, "sodium_mg": 0, "carbs_g": 0, "sugar_g": 0
    }):
        text, total = _breakdown_summary_text(pending)

    assert "Logged 1/1" in text
    assert "eggs" in text
    # Lookup-based: 150 kcal/100g * 120g = 180 kcal
    assert total["calories"] == pytest.approx(180)
    # AI estimate comparison should appear
    assert "AI whole-meal estimate" in text


def test_breakdown_summary_uses_claude_when_no_lookup():
    pending = {
        "ingredients": [
            {
                "name": "homemade salsa",
                "status": "agreed",
                "final_grams": 50,
                "lookup_found": False,
                "claude_calories": 20,
                "claude_carbs_g": 4,
                "claude_sat_fat_g": 0,
                "claude_sodium_mg": 200,
                "claude_sugar_g": 2,
                "claude_fiber_g": 1,
            },
        ],
        "ai_estimate": {},
        "date": "2026-01-01",
    }
    with patch("nutrition_handlers.db.get_day_totals", return_value={
        "calories": 0, "sat_fat_g": 0, "sodium_mg": 0, "carbs_g": 0, "sugar_g": 0
    }):
        text, total = _breakdown_summary_text(pending)

    assert total["calories"] == pytest.approx(20)
    assert "homemade salsa" in text


def test_breakdown_summary_skipped_ingredients_listed():
    pending = {
        "ingredients": [
            {"name": "eggs",   "status": "agreed",  "final_grams": 100, "lookup_found": False,
             "claude_calories": 155, "claude_carbs_g": 1, "claude_sat_fat_g": 3,
             "claude_sodium_mg": 100, "claude_sugar_g": 0, "claude_fiber_g": 0},
            {"name": "butter", "status": "skipped", "final_grams": 10, "lookup_found": False,
             "claude_calories": 72, "claude_carbs_g": 0, "claude_sat_fat_g": 5,
             "claude_sodium_mg": 50, "claude_sugar_g": 0, "claude_fiber_g": 0},
        ],
        "ai_estimate": {},
        "date": "2026-01-01",
    }
    with patch("nutrition_handlers.db.get_day_totals", return_value={
        "calories": 0, "sat_fat_g": 0, "sodium_mg": 0, "carbs_g": 0, "sugar_g": 0
    }):
        text, total = _breakdown_summary_text(pending)

    assert "Skipped" in text
    assert "butter" in text
    # Only eggs should be in total
    assert total["calories"] == pytest.approx(155)


# ---------------------------------------------------------------------------
# C5 — _progress_bar()
# ---------------------------------------------------------------------------

def test_progress_bar_full():
    bar = _progress_bar(2000, 2000)
    assert "100%" in bar
    assert "░" not in bar


def test_progress_bar_half():
    bar = _progress_bar(1000, 2000)
    assert "50%" in bar


def test_progress_bar_over_limit():
    bar = _progress_bar(3000, 2000)
    assert "100%" in bar  # clamped at 1.0
