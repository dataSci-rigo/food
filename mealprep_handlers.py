"""
Meal prep handlers — fridge inventory for thread THREAD_MEALPREP.
When user says "I ate X", deducts from fridge AND logs to nutrition DB.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import mealprep_db
import db as nutrition_db
import gi as gi_mod
from nutrition import lookup as nutrition_lookup

log = logging.getLogger(__name__)

router = Router()
_CHAN  = F.chat.id == config.CHANNEL_ID
_THR   = F.message_thread_id == config.THREAD_MEALPREP


# ---------------------------------------------------------------------------
# Claude intent extraction
# ---------------------------------------------------------------------------

_INTENT_SYSTEM = """\
You are a fridge / meal prep tracker. Extract intent and items from the user's message.
Return ONLY valid JSON:
{
  "intent": "add" | "eat" | "remove" | "show" | "other",
  "items": [
    {"name": "chicken breast", "quantity": 500, "unit": "g"}
  ]
}
- "add"   : user bought or stocked food (push to fridge)
- "eat"   : user consumed something from the fridge (reduce fridge AND log to nutrition)
- "remove": user discarded food without eating (spoiled, gave away)
- "show"  : user wants to see fridge contents
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
    items = mealprep_db.get_fridge()
    if not items:
        return "🧊 Fridge is empty."
    lines = ["🧊 *Fridge*"]
    for item in items:
        lines.append(f"  • {item['item_name']}: {item['quantity']:.0f} {item['unit']}")
    return "\n".join(lines)


async def _log_to_nutrition(name: str, quantity: float, unit: str, user_input: str) -> str:
    """Log eaten food to nutrition DB. Returns a summary line."""
    grams = None
    if unit in ("g", "kg"):
        grams = quantity if unit == "g" else quantity * 1000
    elif unit == "ml":
        grams = quantity  # approximate 1ml ≈ 1g

    result = nutrition_lookup(name)
    if result is None:
        return f"  (nutrition: {name} not found in DB)"

    if grams is None and result.basis == "per_serving" and result.serving_g:
        grams = result.serving_g

    if grams and result.basis == "per_100g":
        scale = grams / 100
        nutrients = {
            "calories":  (result.calories or 0) * scale,
            "sat_fat_g": (result.saturated_fat_g or 0) * scale,
            "sodium_mg": (result.sodium_mg or 0) * scale,
            "carbs_g":   (result.carbs_g or 0) * scale,
            "sugar_g":   (result.sugar_g or 0) * scale,
            "fiber_g":   (result.fiber_g or 0) * scale,
        }
    elif result.basis == "per_serving" and result.serving_g:
        scale = (grams or result.serving_g) / result.serving_g
        nutrients = {
            "calories":  (result.calories or 0) * scale,
            "sat_fat_g": (result.saturated_fat_g or 0) * scale,
            "sodium_mg": (result.sodium_mg or 0) * scale,
            "carbs_g":   (result.carbs_g or 0) * scale,
            "sugar_g":   (result.sugar_g or 0) * scale,
            "fiber_g":   (result.fiber_g or 0) * scale,
        }
    else:
        return f"  (nutrition: couldn't determine serving size for {name})"

    gi_val, gi_src = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: gi_mod.lookup_gi(
            result.name,
            carbs_g=result.carbs_g,
            sugar_g=result.sugar_g,
            fiber_g=result.fiber_g,
        ),
    )

    nutrition_db.log_food(
        date=_today(), user_input=user_input, food_name=result.name,
        source=result.source, grams=grams, nutrients=nutrients,
        gi=gi_val, gi_source=gi_src,
    )

    cal = nutrients.get("calories", 0)
    gi_str = f"GI {gi_val:.0f}" if gi_val is not None else "GI —"
    return f"  → nutrition: {cal:.0f} kcal, {gi_str}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
🧊 *Meal Prep*

Tell me what you bought or ate in plain text:
  "Bought 2 lbs chicken breast and a dozen eggs"
  "Got 500g salmon and 1kg broccoli"
  "I ate the chicken" — deducts from fridge + logs to nutrition
  "Threw out the leftover rice" — remove without nutrition log

/fridge — show fridge contents

When you eat something, I'll auto-log it to your nutrition topic too.
"""


@router.message(_CHAN, _THR, Command("help"))
@router.message(_CHAN, _THR, Command("start"))
async def cmd_help(msg: Message):
    await msg.reply(_HELP_TEXT, parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("fridge"))
async def cmd_fridge(msg: Message):
    await msg.reply(_fridge_text(), parse_mode="Markdown")


@router.message(_CHAN, _THR, F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    try:
        data = await _extract_intent(msg.text)
    except Exception as e:
        log.exception("Meal prep intent extraction failed")
        await msg.reply(f"Couldn't parse that: {e}")
        return

    intent = data.get("intent", "other")
    items  = data.get("items", [])

    if intent == "show":
        await msg.reply(_fridge_text(), parse_mode="Markdown")
        return

    if intent == "add":
        lines = ["Added to fridge:"]
        for item in items:
            name = item["name"]
            qty  = float(item.get("quantity") or 0)
            unit = item.get("unit", "g")
            mealprep_db.add_item(name, qty, unit)
            lines.append(f"  + {qty:.0f} {unit} {name}")
        lines.append("")
        lines.append(_fridge_text())
        await msg.reply("\n".join(lines), parse_mode="Markdown")
        return

    if intent == "eat":
        lines = ["Eaten:"]
        for item in items:
            name  = item["name"]
            qty   = float(item.get("quantity") or 0) or None
            unit  = item.get("unit", "g")
            found, qty_eaten, unit_used = mealprep_db.eat_item(name, qty)
            if found:
                lines.append(f"  - {qty_eaten:.0f} {unit_used} {name} (removed from fridge)")
                nutr_line = await _log_to_nutrition(name, qty_eaten, unit_used, msg.text)
                lines.append(nutr_line)
            else:
                lines.append(f"  ⚠ {name} not in fridge — logging nutrition only")
                if qty:
                    nutr_line = await _log_to_nutrition(name, qty, unit, msg.text)
                    lines.append(nutr_line)
        await msg.reply("\n".join(lines))
        return

    if intent == "remove":
        lines = ["Removed from fridge:"]
        for item in items:
            name  = item["name"]
            qty   = float(item.get("quantity") or 0) or None
            found = mealprep_db.remove_item(name, qty)
            status = "removed" if found else "not found in fridge"
            lines.append(f"  - {name}: {status}")
        await msg.reply("\n".join(lines))
        return

    await msg.reply("Tell me what you bought (\"got 2 lbs chicken\"), ate, or removed. Use /fridge to see inventory.")
