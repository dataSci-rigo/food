"""Unit tests for nutrition.py — all HTTP calls are mocked."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

import nutrition
from nutrition import (
    FoodResult,
    _f,
    detect_kind,
    lookup,
    usda_search,
    openfoodfacts_by_barcode,
    apininjas_nutrition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(payload: dict | list, status_code: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = payload
    m.raise_for_status = MagicMock()
    return m


_USDA_FOOD = {
    "description": "Bananas, raw",
    "servingSize": None,
    "foodNutrients": [
        {"nutrientNumber": "1008", "value": 89},   # calories
        {"nutrientNumber": "1003", "value": 1.1},  # protein
        {"nutrientNumber": "1004", "value": 0.3},  # fat
        {"nutrientNumber": "1258", "value": 0.1},  # sat fat
        {"nutrientNumber": "1005", "value": 22.8}, # carbs
        {"nutrientNumber": "2000", "value": 12.2}, # sugar
        {"nutrientNumber": "1079", "value": 2.6},  # fiber
        {"nutrientNumber": "1093", "value": 1.0},  # sodium
    ],
}

_OFF_PRODUCT = {
    "status": 1,
    "product": {
        "product_name": "Test Snack Bar",
        "serving_quantity": "40",
        "nutriments": {
            "energy-kcal_100g": 450,
            "proteins_100g": 6,
            "fat_100g": 20,
            "saturated-fat_100g": 8,
            "carbohydrates_100g": 60,
            "sugars_100g": 25,
            "fiber_100g": 3,
            "sodium_100g": 0.3,   # grams → will be * 1000 by parser
        },
    },
}

_NINJAS_ITEMS = [
    {
        "name": "eggs",
        "calories": 155,
        "protein_g": 13,
        "fat_total_g": 11,
        "fat_saturated_g": 3.3,
        "carbohydrates_total_g": 1.1,
        "sugar_g": 1.1,
        "fiber_g": 0,
        "sodium_mg": 124,
        "serving_size_g": 100,
    },
    {
        "name": "toast",
        "calories": 270,
        "protein_g": 9,
        "fat_total_g": 3,
        "fat_saturated_g": 0.6,
        "carbohydrates_total_g": 50,
        "sugar_g": 5,
        "fiber_g": 2.5,
        "sodium_mg": 490,
        "serving_size_g": 100,
    },
]


# ---------------------------------------------------------------------------
# B1 — _f() coercion helper
# ---------------------------------------------------------------------------

def test_f_helper_coerces_string():
    assert _f("1.7 g") == pytest.approx(1.7)


def test_f_helper_handles_none():
    assert _f(None) is None


def test_f_helper_handles_int():
    assert _f(42) == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# B2 — detect_kind()
# ---------------------------------------------------------------------------

def test_detect_kind_barcode():
    assert detect_kind("737628064502") == "barcode"


def test_detect_kind_freeform_with_quantity():
    assert detect_kind("2 eggs and toast") == "freeform"


def test_detect_kind_freeform_with_and():
    assert detect_kind("chicken and rice") == "freeform"


def test_detect_kind_name():
    assert detect_kind("banana") == "name"


# ---------------------------------------------------------------------------
# B3 — lookup() routing
# ---------------------------------------------------------------------------

def test_lookup_routes_barcode():
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response(_OFF_PRODUCT)
        result = lookup("737628064502")
    assert result is not None
    assert result.source == "openfoodfacts"


def test_lookup_routes_name_to_usda():
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response({"foods": [_USDA_FOOD]})
        result = lookup("banana")
    assert result is not None
    assert result.source == "usda"


def test_lookup_freeform_no_key_falls_back_to_usda(monkeypatch):
    monkeypatch.setattr(nutrition, "API_NINJAS_API_KEY", "")
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response({"foods": [_USDA_FOOD]})
        result = lookup("2 eggs and toast")
    assert result is not None
    assert result.source == "usda"


def test_lookup_freeform_apininjas_exception_falls_back(monkeypatch):
    monkeypatch.setattr(nutrition, "API_NINJAS_API_KEY", "fake-key")
    call_count = {"n": 0}
    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("network error")
        return _mock_response({"foods": [_USDA_FOOD]})
    with patch("nutrition.requests.get", side_effect=side_effect):
        result = lookup("2 eggs and toast")
    assert result is not None
    assert result.source == "usda"


# ---------------------------------------------------------------------------
# B4 — provider-level edge cases
# ---------------------------------------------------------------------------

def test_usda_returns_none_on_empty():
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response({"foods": []})
        result = usda_search("nonexistent food xyz123")
    assert result is None


def test_openfoodfacts_returns_none_on_not_found():
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response({"status": 0})
        result = openfoodfacts_by_barcode("000000000000")
    assert result is None


def test_openfoodfacts_sodium_converted_to_mg():
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response(_OFF_PRODUCT)
        result = openfoodfacts_by_barcode("737628064502")
    assert result is not None
    assert result.sodium_mg == pytest.approx(300.0)  # 0.3g * 1000


def test_apininjas_sums_multiple_items(monkeypatch):
    monkeypatch.setattr(nutrition, "API_NINJAS_API_KEY", "fake-key")
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response(_NINJAS_ITEMS)
        result = apininjas_nutrition("2 eggs and toast")
    assert result is not None
    assert result.calories == pytest.approx(155 + 270, abs=1)
    assert result.sodium_mg == pytest.approx(124 + 490, abs=1)
    assert result.source == "apininjas"


def test_usda_parses_nutrient_values():
    with patch("nutrition.requests.get") as mock_get:
        mock_get.return_value = _mock_response({"foods": [_USDA_FOOD]})
        result = usda_search("banana")
    assert result is not None
    assert result.calories == pytest.approx(89)
    assert result.carbs_g == pytest.approx(22.8)
    assert result.basis == "per_100g"
