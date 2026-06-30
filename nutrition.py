"""
nutrition.py — one lookup() interface over three free nutrition sources.

Routing (auto):
    barcode  (8-14 digit string)        -> Open Food Facts   (no key needed)
    freeform ("2 eggs and toast")       -> API Ninjas        (NLP parsing)
    plain name ("chicken thigh")        -> USDA FoodData      (authoritative)

Every provider returns the same FoodResult shape, so the rest of your app
never branches on which source answered. To "reduce capabilities" later,
just delete a provider and its branch in lookup() — nothing else changes.

Keys (set as env vars, or pass into the provider):
    USDA_API_KEY        from https://fdc.nal.usda.gov/api-key-signup.html
                        (DEMO_KEY works for light testing, low rate limit)
    API_NINJAS_API_KEY  from https://api-ninjas.com (free tier)
Open Food Facts needs no key.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from typing import Optional

import requests

USDA_API_KEY = os.environ.get("USDA_API_KEY", "DEMO_KEY")
API_NINJAS_API_KEY = os.environ.get("API_NINJAS_API_KEY", "")
TIMEOUT = 10
# Identify yourself to Open Food Facts per their API etiquette.
HEADERS = {"User-Agent": "nutrition-mvp/0.1 (contact@example.com)"}


# --------------------------------------------------------------------------
# Normalized output: all per the given serving/quantity, not per 100g,
# except where a source only gives per-100g (noted in `basis`).
# --------------------------------------------------------------------------
@dataclass
class FoodResult:
    name: str
    source: str                       # "openfoodfacts" | "usda" | "apininjas"
    basis: str                        # "per_serving" | "per_100g"
    calories: Optional[float] = None  # kcal
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    saturated_fat_g: Optional[float] = None
    carbs_g: Optional[float] = None
    sugar_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    serving_g: Optional[float] = None
    raw: Optional[dict] = None        # original payload, for debugging

    def to_dict(self, include_raw: bool = False) -> dict:
        d = asdict(self)
        if not include_raw:
            d.pop("raw", None)
        return d


def _f(v):
    """Coerce a value (possibly '1.7 g' or None) to a float, or None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


# --------------------------------------------------------------------------
# Provider 1: Open Food Facts  (barcodes / packaged products, no key)
# --------------------------------------------------------------------------
def openfoodfacts_by_barcode(barcode: str) -> Optional[FoodResult]:
    url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != 1:
        return None  # not found
    p = data["product"]
    n = p.get("nutriments", {})
    sodium_mg = _f(n.get("sodium_100g"))
    if sodium_mg is not None:
        sodium_mg *= 1000  # OFF gives sodium in g/100g
    return FoodResult(
        name=p.get("product_name") or p.get("generic_name") or barcode,
        source="openfoodfacts",
        basis="per_100g",
        calories=_f(n.get("energy-kcal_100g")),
        protein_g=_f(n.get("proteins_100g")),
        fat_g=_f(n.get("fat_100g")),
        saturated_fat_g=_f(n.get("saturated-fat_100g")),
        carbs_g=_f(n.get("carbohydrates_100g")),
        sugar_g=_f(n.get("sugars_100g")),
        fiber_g=_f(n.get("fiber_100g")),
        sodium_mg=sodium_mg,
        serving_g=_f(p.get("serving_quantity")),
        raw=p,
    )


# --------------------------------------------------------------------------
# Provider 2: USDA FoodData Central  (plain food names, authoritative)
# --------------------------------------------------------------------------
# Match by nutrient number (stable) with a name fallback.
_USDA_MAP = {
    "calories": ("1008", "energy"),
    "protein_g": ("1003", "protein"),
    "fat_g": ("1004", "total lipid"),
    "saturated_fat_g": ("1258", "fatty acids, total saturated"),
    "carbs_g": ("1005", "carbohydrate"),
    "sugar_g": ("2000", "sugars, total"),
    "fiber_g": ("1079", "fiber"),
    "sodium_mg": ("1093", "sodium"),
}


def usda_search(query: str) -> Optional[FoodResult]:
    url = "https://api.nal.usda.gov/fdc/v1/foods/search"
    params = {"query": query, "pageSize": 1, "api_key": USDA_API_KEY}
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    foods = r.json().get("foods", [])
    if not foods:
        return None
    food = foods[0]

    # Build a lookup from this food's nutrients by number and by name.
    by_num, by_name = {}, {}
    for fn in food.get("foodNutrients", []):
        val = fn.get("value")
        num = str(fn.get("nutrientNumber") or fn.get("number") or "")
        name = (fn.get("nutrientName") or fn.get("name") or "").lower()
        if num:
            by_num[num] = val
        if name:
            by_name[name] = val

    def pick(num, name_sub):
        if num in by_num:
            return _f(by_num[num])
        for nm, val in by_name.items():
            if name_sub in nm:
                return _f(val)
        return None

    fields = {k: pick(num, sub) for k, (num, sub) in _USDA_MAP.items()}
    # USDA Foundation/SR values are per 100g; Branded use labelNutrients.
    return FoodResult(
        name=food.get("description", query),
        source="usda",
        basis="per_100g",
        serving_g=_f(food.get("servingSize")),
        raw=food,
        **fields,
    )


# --------------------------------------------------------------------------
# Provider 3: API Ninjas  (freeform "1lb brisket and fries", NLP)
# --------------------------------------------------------------------------
def apininjas_nutrition(query: str) -> Optional[FoodResult]:
    if not API_NINJAS_API_KEY:
        raise RuntimeError("API_NINJAS_API_KEY not set")
    url = "https://api.api-ninjas.com/v1/nutrition"
    r = requests.get(
        url,
        params={"query": query},
        headers={**HEADERS, "X-Api-Key": API_NINJAS_API_KEY},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    items = r.json()
    if not items:
        return None
    # Freeform input can return several items (one per food) — sum them.
    def s(key):
        vals = [_f(i.get(key)) for i in items]
        vals = [v for v in vals if v is not None]
        return round(sum(vals), 2) if vals else None
    return FoodResult(
        name=", ".join(i.get("name", "") for i in items) or query,
        source="apininjas",
        basis="per_serving",
        calories=s("calories"),
        protein_g=s("protein_g"),
        fat_g=s("fat_total_g"),
        saturated_fat_g=s("fat_saturated_g"),
        carbs_g=s("carbohydrates_total_g"),
        sugar_g=s("sugar_g"),
        fiber_g=s("fiber_g"),
        sodium_mg=s("sodium_mg"),
        serving_g=s("serving_size_g"),
        raw={"items": items},
    )


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
_BARCODE_RE = re.compile(r"^\d{8,14}$")
# "freeform" heuristic: a quantity token, or multiple foods joined by 'and'/','
_FREEFORM_RE = re.compile(r"\b\d|\band\b|,", re.IGNORECASE)


def detect_kind(query: str) -> str:
    q = query.strip()
    if _BARCODE_RE.match(q):
        return "barcode"
    if _FREEFORM_RE.search(q):
        return "freeform"
    return "name"


def lookup(query: str, kind: str = "auto") -> Optional[FoodResult]:
    """
    kind: "auto" | "barcode" | "freeform" | "name"
    Returns a FoodResult or None if nothing matched.
    Falls back to USDA when API Ninjas key is missing or fails.
    """
    if kind == "auto":
        kind = detect_kind(query)

    if kind == "barcode":
        return openfoodfacts_by_barcode(query)
    if kind == "freeform":
        if not API_NINJAS_API_KEY:
            return usda_search(query)
        try:
            return apininjas_nutrition(query)
        except Exception:
            return usda_search(query)
    if kind == "name":
        return usda_search(query)
    raise ValueError(f"unknown kind: {kind}")


if __name__ == "__main__":
    import json
    import sys
    q = " ".join(sys.argv[1:]) or "chicken thigh"
    res = lookup(q)
    print(json.dumps(res.to_dict() if res else None, indent=2))
