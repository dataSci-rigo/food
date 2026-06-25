"""
Maintain COOKING.md — a file listing foods the user likes.
Categorizes by nutrient profile and keeps entries deduplicated.
"""

from __future__ import annotations

import os
import re

import config

_CATEGORIES = ["Proteins", "Carbs", "Fats & Oils", "Fruits", "Vegetables", "Snacks & Sweets", "Other"]

_FRUIT_WORDS = ["apple", "banana", "orange", "grape", "berry", "mango", "peach",
                "pear", "cherry", "kiwi", "melon", "pineapple", "fruit", "plum",
                "apricot", "fig", "date", "lychee", "papaya", "guava"]
_VEG_WORDS = ["broccoli", "spinach", "kale", "carrot", "lettuce", "tomato",
              "pepper", "onion", "garlic", "mushroom", "zucchini", "cabbage",
              "celery", "cucumber", "vegetable", "asparagus", "beet", "cauliflower",
              "eggplant", "artichoke", "leek", "radish", "squash", "yam"]
_PROTEIN_WORDS = ["chicken", "beef", "pork", "fish", "salmon", "tuna", "turkey",
                  "egg", "steak", "lamb", "shrimp", "tofu", "tempeh", "lentil",
                  "bean", "legume", "cod", "tilapia", "sardine", "anchovy", "crab",
                  "lobster", "scallop", "venison", "bison", "duck", "quail"]
_FAT_WORDS = ["avocado", "olive oil", "butter", "ghee", "almond", "walnut",
              "peanut", "cashew", "pistachio", "pecan", "macadamia", "hazelnut",
              "chia", "flax", "hemp seed", "coconut oil", "lard", "tallow"]


def _categorize(food_name: str, nutrients: dict) -> str:
    cal = nutrients.get("calories") or 0
    carbs = nutrients.get("carbs_g") or 0
    sugar = nutrients.get("sugar_g") or 0

    name_lower = food_name.lower()

    if any(w in name_lower for w in _FRUIT_WORDS):
        return "Fruits"
    if any(w in name_lower for w in _VEG_WORDS):
        return "Vegetables"
    if any(w in name_lower for w in _PROTEIN_WORDS):
        return "Proteins"
    if any(w in name_lower for w in _FAT_WORDS):
        return "Fats & Oils"
    # carbs_g * 4 kcal/g gives cal-from-carbs; > 50% of total → carb-dominant
    if cal > 0 and carbs * 4 / cal > 0.5:
        return "Carbs"
    if sugar and sugar > 10:
        return "Snacks & Sweets"
    return "Other"


def _read() -> str:
    if os.path.exists(config.COOKING_MD):
        with open(config.COOKING_MD) as f:
            return f.read()
    return "# Foods I Like\n"


def _write(content: str):
    os.makedirs(os.path.dirname(config.COOKING_MD), exist_ok=True)
    with open(config.COOKING_MD, "w") as f:
        f.write(content)


def _entry_line(food_name: str, nutrients: dict, gi: float | None) -> str:
    parts = []
    if nutrients.get("calories"):
        grams = nutrients.get("grams_eaten")
        parts.append(f"~{nutrients['calories']:.0f} kcal" + (f"/{grams:.0f}g" if grams else ""))
    if gi is not None:
        parts.append(f"GI {gi:.0f}")
    suffix = f" — {', '.join(parts)}" if parts else ""
    return f"- {food_name}{suffix}"


def add_liked_food(food_name: str, nutrients: dict, gi: float | None = None):
    content = _read()
    # Skip if already listed
    if food_name.lower() in content.lower():
        return

    category = _categorize(food_name, nutrients)
    new_line = _entry_line(food_name, nutrients, gi)

    section_header = f"## {category}"
    if section_header in content:
        # Insert after the section header
        content = content.replace(
            section_header,
            f"{section_header}\n{new_line}",
            1,
        )
    else:
        # Append new section at end
        content = content.rstrip() + f"\n\n{section_header}\n{new_line}\n"

    _write(content)


def remove_liked_food(food_name: str):
    content = _read()
    name_lower = food_name.lower()
    lines = content.splitlines(keepends=True)
    new_lines = [
        line for line in lines
        if not (line.strip().startswith("- ") and name_lower in line.lower())
    ]
    _write("".join(new_lines))


def get_cooking_md() -> str:
    return _read()
