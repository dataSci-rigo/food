"""
Fridge / recipe search handlers -- thread THREAD_FRIDGE.

Takes over from mealprep_handlers.py: same "tell me what you bought/ate"
free-text flow (Claude intent extraction, same shape as mealprep_handlers.py
and nutrition_handlers.py), plus /cook to rank the 163k-recipe corpus in
fridge_recipes/ against current inventory.

Inventory now lives in fridge_recipes' app.db (core.inventory), not
mealprep.db -- see fridge_recipes/etl/migrate_mealprep.py for the one-time
migration. "Eating" something still decrements inventory AND logs to the
nutrition DB (db.log_food), same bridge mealprep_handlers.py had, so the
existing calorie budget/history features keep working on one timeline.

fridge_recipes is installed as an editable dependency of this project (see
requirements.txt: `-e ../fridge_recipes`) so its core/ modules import
directly here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import db as nutrition_db
import gi as gi_mod
from nutrition import lookup as nutrition_lookup

from core.db import connect as fridge_connect
from core.config import APP_DB_PATH, RECIPES_DB_PATH
from core import inventory as fridge_inventory
from core.models import InventoryItem, RankFilters
from core.nutrition import lookup as ingredient_nutrition_lookup
from core.rank import rank_recipes
from core.search import get_recipe, load_candidate_recipes

log = logging.getLogger(__name__)

router = Router()
_CHAN = F.chat.id == config.CHANNEL_ID
_THR = F.message_thread_id == getattr(config, "THREAD_FRIDGE", config.THREAD_MEALPREP)


# ---------------------------------------------------------------------------
# Claude intent extraction (same shape as mealprep_handlers.py)
# ---------------------------------------------------------------------------

_INTENT_SYSTEM = """\
You are a fridge / recipe tracker. Extract intent and items from the user's message.
Return ONLY valid JSON:
{
  "intent": "add" | "eat" | "remove" | "show" | "cook" | "other",
  "items": [
    {"name": "chicken breast", "quantity": 500, "unit": "g"}
  ]
}
- "add"   : user bought or stocked food (push to fridge)
- "eat"   : user consumed something from the fridge (reduce fridge AND log to nutrition)
- "remove": user discarded food without eating (spoiled, gave away)
- "show"  : user wants to see fridge contents
- "cook"  : user is asking what they can make / wants recipe suggestions
- "other" : unrelated
For "eat" without explicit quantity, use a reasonable serving size and note it.
Use "g" for solids, "ml" for liquids, "count" for items (eggs, apples, etc.).
No prose, no markdown."""


async def _extract_intent(text: str) -> dict:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=_INTENT_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _fridge_text() -> str:
    items = fridge_inventory.list_inventory(db_path=APP_DB_PATH)
    if not items:
        return "🧊 Fridge is empty."
    lines = ["🧊 *Fridge*"]
    for item in items:
        qty = f" ({item['qty_text']})" if item["qty_text"] else ""
        flag = " ⚠️" if item["state"] in ("use_soon", "expired") else ""
        lines.append(f"  • {item['canonical_ingredient']}{qty}{flag}")
    return "\n".join(lines)


def _qty_to_grams(qty_text: str | None) -> float | None:
    """Best-effort: only handles a leading numeric gram/kg/ml amount."""
    if not qty_text:
        return None
    m = re.match(r"([\d.]+)\s*(g|kg|ml)?", qty_text.strip(), re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "g").lower()
    return value * 1000 if unit == "kg" else value


async def _log_eaten_item_to_nutrition(name: str, qty_text: str | None, user_input: str) -> str:
    """Same bridge mealprep_handlers.py had: log an eaten fridge item to the
    shared nutrition DB so /today, /budget, and the web /food page keep
    working. Tries the fridge_recipes ingredient_nutrition cache first
    (bulk-backfilled + lazily live-cached), falling back to food/nutrition.py's
    cascade if that ingredient has no data at all."""
    grams = _qty_to_grams(qty_text)

    per100 = await asyncio.get_event_loop().run_in_executor(
        None, ingredient_nutrition_lookup, name
    )
    if per100 is not None and per100.cal_per_100g is not None:
        g = grams or 100.0
        scale = g / 100.0
        nutrients = {
            "calories": (per100.cal_per_100g or 0) * scale,
            "sat_fat_g": (per100.sat_fat_g or 0) * scale,
            "sodium_mg": (per100.sodium_mg or 0) * scale,
            "carbs_g": (per100.carbs_g or 0) * scale,
            "sugar_g": (per100.sugar_g or 0) * scale,
            "fiber_g": (per100.fiber_g or 0) * scale,
        }
        source = per100.source
    else:
        result = await asyncio.get_event_loop().run_in_executor(
            None, nutrition_lookup, name
        )
        if result is None:
            return f"  (nutrition: {name} not found)"
        g = grams or result.serving_g or 100.0
        scale = g / 100.0 if result.basis == "per_100g" else (g / result.serving_g if result.serving_g else 1.0)
        nutrients = {
            "calories": (result.calories or 0) * scale,
            "sat_fat_g": (result.saturated_fat_g or 0) * scale,
            "sodium_mg": (result.sodium_mg or 0) * scale,
            "carbs_g": (result.carbs_g or 0) * scale,
            "sugar_g": (result.sugar_g or 0) * scale,
            "fiber_g": (result.fiber_g or 0) * scale,
        }
        source = result.source

    gi_val, gi_src = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: gi_mod.lookup_gi(
            name, carbs_g=nutrients["carbs_g"], sugar_g=nutrients["sugar_g"],
            fiber_g=nutrients["fiber_g"],
        ),
    )

    nutrition_db.log_food(
        date=_today(), user_input=user_input, food_name=name,
        source=source, grams=grams, nutrients=nutrients,
        gi=gi_val, gi_source=gi_src,
    )
    cal = nutrients.get("calories", 0)
    gi_str = f"GI {gi_val:.0f}" if gi_val is not None else "GI —"
    return f"  → nutrition: {cal:.0f} kcal, {gi_str}"


def _log_cooked(recipe_id: int | None, custom_recipe_id: int | None, notes: str | None = None) -> None:
    conn = fridge_connect(APP_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO cooked_log (recipe_id, custom_recipe_id, cooked_at, notes) "
            "VALUES (?, ?, ?, ?)",
            (recipe_id, custom_recipe_id, datetime.now(timezone.utc).isoformat(), notes),
        )
        conn.commit()
    finally:
        conn.close()


def _rate_recipe(recipe_id: int, stars: int = 5, notes: str | None = None) -> None:
    conn = fridge_connect(APP_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO ratings (recipe_id, stars, cooked_at, notes) VALUES (?, ?, ?, ?)",
            (recipe_id, stars, datetime.now(timezone.utc).isoformat(), notes),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
🧊 *Fridge & Recipes*

Tell me what you bought or ate in plain text:
  "Bought 2 lbs chicken breast and a dozen eggs"
  "I ate the chicken" — deducts from fridge + logs to nutrition
  "Threw out the leftover rice" — remove without nutrition log
  "What can I make?" — recipe suggestions from your fridge

/fridge — show fridge contents
/cook — top recipe suggestions from what's in your fridge
/recipe <id> — full recipe detail (ingredients you have/miss, directions)
/save <id> [stars] — bookmark a recipe you liked (default 5★)

When you eat something, I'll auto-log it to your nutrition topic too.
"""


@router.message(_CHAN, _THR, Command("help"))
@router.message(_CHAN, _THR, Command("start"))
async def cmd_help(msg: Message):
    await msg.reply(_HELP_TEXT, parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("fridge"))
async def cmd_fridge(msg: Message):
    await msg.reply(_fridge_text(), parse_mode="Markdown")


def _cook_reply(top_n: int = 5) -> str:
    items = fridge_inventory.list_inventory(db_path=APP_DB_PATH)
    if not items:
        return "🧊 Fridge is empty — add something first with /fridge or tell me what you bought."
    inv = [InventoryItem(i["canonical_ingredient"], state=i["state"]) for i in items]
    names = {i.canonical_ingredient for i in inv}
    recipes = load_candidate_recipes(names, db_path=RECIPES_DB_PATH)
    results = rank_recipes(recipes, inv, filters=RankFilters(), top_n=top_n)
    if not results:
        return "Couldn't find a recipe using what's in your fridge right now."
    lines = ["🍳 *Top picks from your fridge:*"]
    for r in results:
        time_str = f", {r.recipe.active_min}m active" if r.recipe.active_min else ""
        lines.append(f"\n*#{r.recipe.id}* {r.recipe.title}{time_str}\n  {r.explanation}")
    lines.append("\nTap /recipe <id> for the full recipe.")
    return "\n".join(lines)


@router.message(_CHAN, _THR, Command("cook"))
async def cmd_cook(msg: Message):
    reply = await asyncio.get_event_loop().run_in_executor(None, _cook_reply)
    await msg.reply(reply, parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("recipe"))
async def cmd_recipe(msg: Message):
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await msg.reply("Usage: /recipe <id> (get an id from /cook)")
        return
    recipe_id = int(parts[1].strip())
    detail = await asyncio.get_event_loop().run_in_executor(None, get_recipe, recipe_id)
    if detail is None:
        await msg.reply(f"No recipe #{recipe_id}.")
        return
    have_names = {
        i["canonical_ingredient"]
        for i in fridge_inventory.list_inventory(db_path=APP_DB_PATH)
    }
    lines = [f"*{detail['title']}* (#{detail['id']})\n"]
    for ing in detail["ingredients"]:
        mark = "✓" if ing["name"] in have_names else "✗"
        qty = f" ({ing['quantity_text']})" if ing["quantity_text"] else ""
        lines.append(f"  {mark} {ing['name']}{qty}")
    if detail["directions"]:
        lines.append(f"\n{detail['directions']}")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("save"))
async def cmd_save(msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply("Usage: /save <id> [stars 1-5]")
        return
    recipe_id = int(parts[1])
    stars = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
    stars = max(1, min(5, stars))
    await asyncio.get_event_loop().run_in_executor(None, _rate_recipe, recipe_id, stars)
    await msg.reply(f"Saved #{recipe_id} at {stars}★.")


@router.message(_CHAN, _THR, F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    try:
        data = await _extract_intent(msg.text)
    except Exception as e:
        log.exception("Fridge intent extraction failed")
        await msg.reply(f"Couldn't parse that: {e}")
        return

    intent = data.get("intent", "other")
    items = data.get("items", [])

    if intent == "show":
        await msg.reply(_fridge_text(), parse_mode="Markdown")
        return

    if intent == "cook":
        reply = await asyncio.get_event_loop().run_in_executor(None, _cook_reply)
        await msg.reply(reply, parse_mode="Markdown")
        return

    if intent == "add":
        lines = ["Added to fridge:"]
        for item in items:
            name = item["name"]
            qty = item.get("quantity")
            unit = item.get("unit", "g")
            qty_text = f"{qty:.0f} {unit}" if qty else None
            fridge_inventory.add_item(name, qty_text, db_path=APP_DB_PATH)
            lines.append(f"  + {qty_text or ''} {name}")
        lines.append("")
        lines.append(_fridge_text())
        await msg.reply("\n".join(lines), parse_mode="Markdown")
        return

    if intent == "eat":
        lines = ["Eaten:"]
        for item in items:
            name = item["name"]
            qty = item.get("quantity")
            unit = item.get("unit", "g")
            qty_text = f"{qty:.0f} {unit}" if qty else None
            removed = fridge_inventory.eat_item(name, db_path=APP_DB_PATH)
            if removed:
                for row in removed:
                    lines.append(f"  - {row['qty_text'] or ''} {row['canonical_ingredient']} (removed from fridge)")
                nutr_line = await _log_eaten_item_to_nutrition(name, qty_text or removed[0]["qty_text"], msg.text)
                lines.append(nutr_line)
            else:
                lines.append(f"  ⚠ {name} not in fridge — logging nutrition only")
                nutr_line = await _log_eaten_item_to_nutrition(name, qty_text, msg.text)
                lines.append(nutr_line)
        await msg.reply("\n".join(lines))
        return

    if intent == "remove":
        lines = ["Removed from fridge:"]
        for item in items:
            name = item["name"]
            removed = fridge_inventory.remove_item(name, db_path=APP_DB_PATH)
            status = f"removed ({len(removed)})" if removed else "not found in fridge"
            lines.append(f"  - {name}: {status}")
        await msg.reply("\n".join(lines))
        return

    await msg.reply(
        "Tell me what you bought, ate, or removed, or ask \"what can I make?\". "
        "Use /fridge to see inventory, /cook for suggestions."
    )
