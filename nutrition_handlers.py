"""
Nutrition handlers — food logging for thread THREAD_NUTRITION.
Adapted from bot.py; uses Router + channel/thread filter instead of private chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import cooking
import db
import gi as gi_mod
import ocr
from nutrition import lookup as nutrition_lookup

log = logging.getLogger(__name__)

router = Router()
_CHAN  = F.chat.id == config.CHANNEL_ID
_THR   = F.message_thread_id == config.THREAD_NUTRITION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _progress_bar(actual: float, limit: float, width: int = 10) -> str:
    pct    = min(actual / limit, 1.0) if limit else 0
    filled = round(pct * width)
    return "▓" * filled + "░" * (width - filled) + f"  {pct*100:.0f}%"


def _format_totals(totals: dict) -> str:
    lim = config.DAILY_LIMITS
    return "\n".join([
        f"Calories:  {totals['calories']:.0f} / {lim['calories']} kcal  "
        f"{_progress_bar(totals['calories'], lim['calories'])}",
        f"Sat fat:   {totals['sat_fat_g']:.1f} / {lim['sat_fat_g']} g",
        f"Sodium:    {totals['sodium_mg']:.0f} / {lim['sodium_mg']} mg",
        f"Carbs:     {totals['carbs_g']:.0f} / {lim['carbs_g']} g",
        f"Sugar:     {totals['sugar_g']:.0f} / {lim['sugar_g']} g",
    ])


def _format_budget(totals: dict) -> str:
    lim = config.DAILY_LIMITS
    def rem(key, unit, dec=0):
        left = lim[key] - totals.get(key, 0)
        flag = " ⚠️" if left < 0 else ""
        return f"{left:.{dec}f} {unit}{flag}"
    return "\n".join([
        "Remaining budget:",
        f"  Calories:  {rem('calories', 'kcal')}",
        f"  Sat fat:   {rem('sat_fat_g', 'g', 1)}",
        f"  Sodium:    {rem('sodium_mg', 'mg')}",
        f"  Carbs:     {rem('carbs_g', 'g')}",
        f"  Sugar:     {rem('sugar_g', 'g')}",
    ])


def _rating_keyboard(log_ids: list[int]) -> InlineKeyboardMarkup:
    ids_str = ",".join(str(i) for i in log_ids)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Liked it",    callback_data=f"n:like:{ids_str}"),
        InlineKeyboardButton(text="👎 Didn't like", callback_data=f"n:dislike:{ids_str}"),
        InlineKeyboardButton(text="— Skip",         callback_data=f"n:skip:{ids_str}"),
    ]])


# ---------------------------------------------------------------------------
# Claude intent extraction
# ---------------------------------------------------------------------------

_INTENT_SYSTEM = """\
You are a food logging assistant. Analyze the user message and return ONLY valid JSON:
{
  "intent": "log" | "query" | "budget" | "check" | "other",
  "foods": ["food string 1", "food string 2"],
  "clarification_needed": false,
  "clarification_question": null
}
- "log": user reports eating something
- "query": user asks what they've eaten today
- "budget": user asks how much nutrition budget remains
- "check": user wants nutrition info without logging
- "other": unrelated
foods: list only if intent is log or check; preserve quantities in strings.
No prose, no markdown."""


async def _extract_intent(text: str) -> dict:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=_INTENT_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# AI double-check
# ---------------------------------------------------------------------------

async def _ai_verify(msg: Message, food_name: str, grams: float | None, nutrients: dict):
    """Sends a follow-up only if Claude flags the nutrition values as implausible."""
    cal  = nutrients.get("calories", 0) or 0
    carbs = nutrients.get("carbs_g", 0) or 0
    sat  = nutrients.get("sat_fat_g", 0) or 0
    per_100 = f"{cal/grams*100:.0f} kcal/100g" if grams and grams > 0 else f"{cal:.0f} kcal total"
    prompt = (
        f"Food: {food_name}\n"
        f"Logged: {grams or '?'}g → {cal:.0f} kcal ({per_100}), "
        f"{carbs:.1f}g carbs, {sat:.1f}g sat fat\n"
        'Are these plausible? Return ONLY JSON: {"ok": true, "note": null} '
        'or {"ok": false, "note": "brief reason"}'
    )
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        data = json.loads(raw)
        if not data.get("ok") and data.get("note"):
            await msg.reply(f"⚠️ Nutrition check: {data['note']}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# AI helpers for comparison flow
# ---------------------------------------------------------------------------

async def _ai_estimate_nutrition(text: str) -> dict:
    """Whole-meal nutrition estimate from Claude."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=250,
        messages=[{"role": "user", "content":
            f"Estimate nutrition for this meal: {text}\n"
            "Assume typical home-cooked portions unless specified.\n"
            'Return ONLY JSON: {"meal_name":"...","assumptions":"...","calories":0,'
            '"carbs_g":0,"sat_fat_g":0,"sodium_mg":0,"sugar_g":0,"fiber_g":0}'
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


async def _ai_breakdown_ingredients(text: str) -> list[dict]:
    """Break a meal description into individual ingredients with quantities."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        messages=[{"role": "user", "content":
            f"Break this meal into individual ingredients with estimates: {text}\n"
            "Assume typical home-cooked portions.\n"
            'Return ONLY a JSON array: [{"name":"2 large eggs fried","grams":120,'
            '"calories":180,"carbs_g":1,"sat_fat_g":5,"sodium_mg":140,"sugar_g":0,"fiber_g":0}]'
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


async def _ai_reestimate_ingredient(original_name: str, user_desc: str) -> dict:
    """Re-estimate a single ingredient given a user-corrected description."""
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content":
            f"Re-estimate nutrition for: {user_desc} (was: {original_name})\n"
            'Return ONLY JSON: {"name":"...","grams":0,"calories":0,'
            '"carbs_g":0,"sat_fat_g":0,"sodium_mg":0,"sugar_g":0,"fiber_g":0}'
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def _lookup_result_to_per100g(result) -> dict | None:
    """Convert a FoodResult to a per-100g nutrient dict, or None if insufficient data."""
    if result is None:
        return None
    if result.basis == "per_100g":
        return {
            "calories":  result.calories,
            "sat_fat_g": result.saturated_fat_g,
            "sodium_mg": result.sodium_mg,
            "carbs_g":   result.carbs_g,
            "sugar_g":   result.sugar_g,
            "fiber_g":   result.fiber_g,
        }
    if result.basis == "per_serving" and result.serving_g:
        scale = 100 / result.serving_g
        return {
            "calories":  (result.calories or 0) * scale,
            "sat_fat_g": (result.saturated_fat_g or 0) * scale,
            "sodium_mg": (result.sodium_mg or 0) * scale,
            "carbs_g":   (result.carbs_g or 0) * scale,
            "sugar_g":   (result.sugar_g or 0) * scale,
            "fiber_g":   (result.fiber_g or 0) * scale,
        }
    return None


def _scale_nutrients(per_100g: dict, grams: float) -> dict:
    scale = grams / 100
    return {k: (v or 0) * scale for k, v in per_100g.items()}


def _breakdown_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Agree",   callback_data="bkd:agree"),
        InlineKeyboardButton(text="✏️ Change",  callback_data="bkd:change"),
        InlineKeyboardButton(text="❌ None",    callback_data="bkd:none"),
    ]])


def _ingredient_text(ing: dict, idx: int, total: int) -> str:
    grams  = ing.get("final_grams") or ing.get("claude_grams") or 0
    ai_cal = ing.get("claude_calories") or 0
    ai_carb= ing.get("claude_carbs_g") or 0
    ai_sat = ing.get("claude_sat_fat_g") or 0

    lines  = [f"*Ingredient {idx+1}/{total}: {ing['name']}* (~{grams:.0f}g)"]

    if ing.get("lookup_found"):
        lup = _scale_nutrients(ing["lookup_per_100g"], grams)
        lines.append(
            f"  📊 Our DB ({ing['lookup_source']}):  "
            f"{lup['calories']:.0f} kcal  •  {lup['carbs_g']:.1f}g carbs  •  {lup['sat_fat_g']:.1f}g sat"
        )
    else:
        lines.append("  📊 Our DB: not found")

    lines.append(
        f"  🤖 AI guess:              "
        f"{ai_cal:.0f} kcal  •  {ai_carb:.1f}g carbs  •  {ai_sat:.1f}g sat"
    )
    return "\n".join(lines)


async def _show_breakdown_ingredient(send_msg: Message, pending: dict):
    ingredients = pending["ingredients"]
    idx         = pending.get("changing_idx", pending["idx"])
    ing         = ingredients[idx]
    await send_msg.answer(
        _ingredient_text(ing, idx, len(ingredients)),
        parse_mode="Markdown",
        reply_markup=_breakdown_keyboard(),
    )


# ---------------------------------------------------------------------------
# Nutrition lookup + logging
# ---------------------------------------------------------------------------

async def _lookup_and_log(
    food_str: str, user_input: str, date: str, grams_override: float | None = None,
) -> tuple[str, int | None]:
    # Check local catalog first
    catalog_hit = db.catalog_search(food_str)
    if catalog_hit:
        name  = catalog_hit["name"]
        grams = grams_override
        if grams is None and catalog_hit.get("serving_g"):
            grams = catalog_hit["serving_g"]
        if grams is None:
            return f"__needs_grams__{name}", None
        scale = grams / 100
        nutrients = {
            "calories":  (catalog_hit.get("cal_per_100g") or 0) * scale,
            "sat_fat_g": (catalog_hit.get("sat_fat_per_100g") or 0) * scale,
            "sodium_mg": (catalog_hit.get("sodium_per_100g") or 0) * scale,
            "carbs_g":   (catalog_hit.get("carbs_per_100g") or 0) * scale,
            "sugar_g":   (catalog_hit.get("sugar_per_100g") or 0) * scale,
            "fiber_g":   (catalog_hit.get("fiber_per_100g") or 0) * scale,
            "_per_100g": {
                "calories":  catalog_hit.get("cal_per_100g"),
                "sat_fat_g": catalog_hit.get("sat_fat_per_100g"),
                "sodium_mg": catalog_hit.get("sodium_per_100g"),
                "carbs_g":   catalog_hit.get("carbs_per_100g"),
                "sugar_g":   catalog_hit.get("sugar_per_100g"),
            },
        }
        gi_val, gi_src = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: gi_mod.lookup_gi(
                name,
                carbs_g=catalog_hit.get("carbs_per_100g"),
                sugar_g=catalog_hit.get("sugar_per_100g"),
                fiber_g=catalog_hit.get("fiber_per_100g"),
            ),
        )
        log_id = db.log_food(
            date=date, user_input=user_input, food_name=name,
            source="catalog", grams=grams, nutrients=nutrients,
            gi=gi_val, gi_source=gi_src,
        )
        gi_str   = f"GI {gi_val:.0f}" if gi_val is not None else "GI —"
        cal      = nutrients.get("calories", 0)
        sat      = nutrients.get("sat_fat_g", 0)
        sod      = nutrients.get("sodium_mg", 0)
        return (f"• {name} ({grams:.0f}g) — {cal:.0f} kcal, {gi_str}, "
                f"{sat:.1f}g sat fat, {sod:.0f}mg sodium  📋"), log_id

    try:
        result = nutrition_lookup(food_str)
    except Exception as e:
        log.warning("nutrition_lookup failed for %r: %s", food_str, e)
        result = None
    if result is None:
        return f"• {food_str} — couldn't find nutrition data", None

    grams    = grams_override
    nutrients: dict = {}

    if result.basis == "per_serving" and result.serving_g:
        grams = grams or result.serving_g
        scale = grams / result.serving_g if grams else 1.0
        nutrients = {
            "calories":  (result.calories or 0) * scale,
            "sat_fat_g": (result.saturated_fat_g or 0) * scale,
            "sodium_mg": (result.sodium_mg or 0) * scale,
            "carbs_g":   (result.carbs_g or 0) * scale,
            "sugar_g":   (result.sugar_g or 0) * scale,
            "fiber_g":   (result.fiber_g or 0) * scale,
            "_per_100g": {
                "calories":  (result.calories or 0) / result.serving_g * 100 if result.serving_g else None,
                "sat_fat_g": (result.saturated_fat_g or 0) / result.serving_g * 100 if result.serving_g else None,
            },
        }
    elif result.basis == "per_100g":
        if grams is None:
            return f"__needs_grams__{result.name}", None
        scale = grams / 100
        nutrients = {
            "calories":  (result.calories or 0) * scale,
            "sat_fat_g": (result.saturated_fat_g or 0) * scale,
            "sodium_mg": (result.sodium_mg or 0) * scale,
            "carbs_g":   (result.carbs_g or 0) * scale,
            "sugar_g":   (result.sugar_g or 0) * scale,
            "fiber_g":   (result.fiber_g or 0) * scale,
            "_per_100g": {
                "calories":  result.calories,
                "sat_fat_g": result.saturated_fat_g,
                "sodium_mg": result.sodium_mg,
                "carbs_g":   result.carbs_g,
                "sugar_g":   result.sugar_g,
            },
        }
    else:
        return f"• {food_str} — unexpected data format", None

    gi_val, gi_src = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: gi_mod.lookup_gi(
            result.name,
            carbs_g=result.carbs_g,
            sugar_g=result.sugar_g,
            fiber_g=result.fiber_g,
        ),
    )

    log_id = db.log_food(
        date=date, user_input=user_input, food_name=result.name,
        source=result.source, grams=grams, nutrients=nutrients,
        gi=gi_val, gi_source=gi_src,
    )

    gi_str   = f"GI {gi_val:.0f}" if gi_val is not None else "GI —"
    gram_str = f" ({grams:.0f}g)" if grams else ""
    cal      = nutrients.get("calories", 0)
    sat      = nutrients.get("sat_fat_g", 0)
    sod      = nutrients.get("sodium_mg", 0)
    return (f"• {result.name}{gram_str} — "
            f"{cal:.0f} kcal, {gi_str}, {sat:.1f}g sat fat, {sod:.0f}mg sodium"), log_id


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
🥗 *Nutrition*

Type what you ate to choose how nutrition is calculated:
  📊 Lookup  •  🤖 AI Estimate  •  🔍 Ingredient Breakdown

Quick log (skips the choice menu):
  /log 2 eggs and toast
  /log 300g chicken breast and rice

Other shortcuts:
  /check banana — look up without logging
  /compare fried eggs with toast — force comparison menu

Send a photo of a nutrition label to log from it.

/today — today's log + totals
/budget — remaining daily allowance
/history — 7-day calorie summary
/catalog — saved nutrition labels
/recipes — saved meal recipes
"""


@router.message(_CHAN, _THR, Command("help"))
@router.message(_CHAN, _THR, Command("start"))
async def cmd_help(msg: Message):
    await msg.reply(_HELP_TEXT, parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("log"))
async def cmd_log(msg: Message):
    """Direct log — skips the comparison choice menu."""
    food_text = (msg.text or "").split(maxsplit=1)
    food_text = food_text[1].strip() if len(food_text) > 1 else ""
    if not food_text:
        await msg.reply("Usage: /log 2 eggs and toast")
        return
    try:
        intent_data = await _extract_intent(food_text)
    except Exception as e:
        await msg.reply(f"Couldn't parse: {e}")
        return
    foods = intent_data.get("foods") or [food_text]
    await _handle_log(msg, foods, food_text)


@router.message(_CHAN, _THR, Command("compare"))
async def cmd_compare(msg: Message):
    """Force the three-way comparison menu for any food text."""
    food_text = (msg.text or "").split(maxsplit=1)
    food_text = food_text[1].strip() if len(food_text) > 1 else ""
    if not food_text:
        await msg.reply("Usage: /compare fried eggs with toast")
        return
    try:
        intent_data = await _extract_intent(food_text)
    except Exception as e:
        await msg.reply(f"Couldn't parse: {e}")
        return
    foods = intent_data.get("foods") or [food_text]
    db.set_state(msg.chat.id, "awaiting_cmp_choice", {
        "user_input": food_text,
        "foods": foods,
        "date": _today(),
    })
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Lookup",       callback_data="cmp:lookup"),
        InlineKeyboardButton(text="🤖 AI Estimate",  callback_data="cmp:ai_est"),
        InlineKeyboardButton(text="🔍 Breakdown",    callback_data="cmp:breakdown"),
    ]])
    await msg.reply(
        f"_{food_text}_\n\nChoose how to estimate nutrition:",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@router.message(_CHAN, _THR, Command("website"))
async def cmd_website(msg: Message):
    ip = config.VM_TAILSCALE_IP
    if not ip:
        await msg.reply("VM_TAILSCALE_IP not set in .env")
        return
    await msg.reply(f"http://{ip}:9000/food")


@router.message(_CHAN, _THR, Command("today"))
async def cmd_today(msg: Message):
    date = _today()
    rows = db.get_day_log(date)
    if not rows:
        await msg.reply("Nothing logged today.")
        return
    lines = [f"*{date}*"]
    for r in rows:
        gi_str  = f"GI {r['glycemic_index']:.0f}" if r["glycemic_index"] is not None else "GI —"
        liked   = " 👍" if r["liked"] == 1 else (" 👎" if r["liked"] == 0 else "")
        time_str = r["logged_at"][11:16] if len(r["logged_at"]) > 15 else ""
        lines.append(f"  {time_str} {r['food_name']} — {(r['calories'] or 0):.0f} kcal, {gi_str}{liked}")
    lines += ["", _format_totals(db.get_day_totals(date))]
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("budget"))
async def cmd_budget(msg: Message):
    await msg.reply(_format_budget(db.get_day_totals(_today())))


@router.message(_CHAN, _THR, Command("history"))
async def cmd_history(msg: Message):
    rows = db.get_history_totals(7)
    if not rows:
        await msg.reply("No history yet.")
        return
    lines = ["*7-day history*"]
    for r in rows:
        pct = int(r["calories"] / config.DAILY_LIMITS["calories"] * 100)
        lines.append(f"  {r['date']}: {r['calories']:.0f} kcal ({pct}%)")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("catalog"))
async def cmd_catalog(msg: Message):
    entries = db.catalog_list()
    if not entries:
        await msg.reply(
            "No foods saved yet.\n\n"
            "Scan a nutrition label photo — after logging I'll offer to save it.",
        )
        return
    lines = ["📋 *Saved nutrition catalog*"]
    for e in entries:
        cal   = e.get("cal_per_100g")
        carbs = e.get("carbs_per_100g")
        serve = e.get("serving_g")
        cal_str   = f"{cal:.0f} kcal" if cal is not None else "? kcal"
        carbs_str = f", {carbs:.0f}g carbs" if carbs is not None else ""
        serve_str = f"  (serving {serve:.0f}g)" if serve else ""
        lines.append(f"  • *{e['name']}* — {cal_str}/100g{carbs_str}{serve_str}")
    lines.append("\nUse /del\\_food name to remove an entry.")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("del_food"))
async def cmd_del_food(msg: Message):
    args = (msg.text or "").split(maxsplit=1)
    name = args[1].strip() if len(args) > 1 else ""
    if not name:
        await msg.reply("Usage: /del\\_food name", parse_mode="Markdown")
        return
    removed = db.catalog_delete(name)
    if removed:
        await msg.reply(f"Removed *{name}* from catalog.", parse_mode="Markdown")
    else:
        await msg.reply(f"*{name}* not found in catalog.", parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Photo handler (OCR)
# ---------------------------------------------------------------------------

@router.message(_CHAN, _THR, F.photo)
async def handle_photo(msg: Message):
    await msg.reply("Reading label...")
    try:
        photo = msg.photo[-1]
        file  = await msg.bot.get_file(photo.file_id)
        bio   = await msg.bot.download_file(file.file_path)
        data  = await ocr.read_label(bio.read())
    except ValueError as e:
        await msg.reply(f"Couldn't read a nutrition label: {e}")
        return
    except Exception:
        log.exception("OCR error")
        await msg.reply("Error reading label.")
        return

    food_name = data.get("food_name") or "Unknown food"
    serving_g = data.get("serving_g")
    lines = [f"*{food_name}*"]
    for key, label in [("calories","Calories"), ("carbs_g","Carbs"),
                       ("sat_fat_g","Sat fat"), ("sodium_mg","Sodium"),
                       ("sugar_g","Sugar"), ("glycemic_index","GI")]:
        val = data.get(key)
        if val is not None:
            lines.append(f"  {label}: {val}")
    if serving_g:
        lines.append(f"  Serving: {serving_g}g")
    lines.append("\nHow many grams did you eat? (or 0 to skip)")

    db.set_state(msg.chat.id, "awaiting_grams", {
        "source": "ocr",
        "foods": [{"food_str": food_name, "user_input": f"[label] {food_name}", "result_data": data}],
        "idx": 0, "log_ids": [],
    })
    await msg.reply("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Free text handler
# ---------------------------------------------------------------------------

@router.message(_CHAN, _THR, F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    state_row = db.get_state(msg.chat.id)
    state     = state_row["state"] if state_row else "idle"

    if state == "awaiting_grams":
        await _handle_grams(msg, state_row["pending"])
        return
    if state == "awaiting_ingredient_change":
        await _handle_ingredient_change(msg, state_row["pending"])
        return
    if state == "awaiting_recipe_name":
        await _handle_save_recipe_name(msg, state_row["pending"])
        return
    if state == "awaiting_rating":
        db.clear_state(msg.chat.id)
        await msg.reply("Rating skipped.")
    if state in ("awaiting_cmp_choice", "breakdown_review"):
        # User typed a new message instead of tapping a button — clear old state
        db.clear_state(msg.chat.id)

    try:
        intent_data = await _extract_intent(msg.text)
    except Exception as e:
        log.exception("Intent extraction failed")
        await msg.reply(f"Couldn't understand that: {e}")
        return

    intent = intent_data.get("intent", "other")
    foods  = intent_data.get("foods", [])

    if intent_data.get("clarification_needed"):
        q = intent_data.get("clarification_question", "Could you clarify?")
        db.set_state(msg.chat.id, "clarifying", {"original": msg.text, "question": q})
        await msg.reply(q)
        return

    if intent == "query":
        await cmd_today(msg)
    elif intent == "budget":
        await cmd_budget(msg)
    elif intent == "check":
        await _handle_check(msg, foods)
    elif intent == "log":
        foods_label = ", ".join(foods) if foods else msg.text
        db.set_state(msg.chat.id, "awaiting_cmp_choice", {
            "user_input": msg.text,
            "foods": foods,
            "date": _today(),
        })
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📊 Lookup",       callback_data="cmp:lookup"),
            InlineKeyboardButton(text="🤖 AI Estimate",  callback_data="cmp:ai_est"),
            InlineKeyboardButton(text="🔍 Breakdown",    callback_data="cmp:breakdown"),
        ]])
        await msg.reply(
            f"Logging: _{foods_label}_\n\nHow should I estimate nutrition?",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    else:
        await msg.reply("I track food! Tell me what you ate, or use /help.")


async def _handle_check(msg: Message, foods: list[str]):
    if not foods:
        await msg.reply("What food do you want to check?")
        return
    lines = []
    for food_str in foods:
        result = nutrition_lookup(food_str)
        if result is None:
            lines.append(f"• {food_str} — not found")
            continue
        gi_val, _ = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: gi_mod.lookup_gi(
                result.name, carbs_g=result.carbs_g,
                sugar_g=result.sugar_g, fiber_g=result.fiber_g,
            ),
        )
        gi_str     = f"GI {gi_val:.0f}" if gi_val is not None else "GI —"
        basis_note = " (per 100g)" if result.basis == "per_100g" else ""
        lines += [
            f"*{result.name}*{basis_note}",
            f"  Calories: {result.calories or '—'} kcal",
            f"  {gi_str}",
            f"  Sat fat: {result.saturated_fat_g or '—'} g",
            f"  Sodium:  {result.sodium_mg or '—'} mg",
            f"  Carbs:   {result.carbs_g or '—'} g",
            f"  Sugar:   {result.sugar_g or '—'} g",
            "",
        ]
    await msg.reply("\n".join(lines), parse_mode="Markdown")


async def _handle_log(msg: Message, foods: list[str], user_input: str, reply_fn=None):
    if reply_fn is None:
        reply_fn = msg.reply
    date         = _today()
    reply_lines  = ["Logged:"]
    log_ids: list[int]  = []
    pending_foods: list[dict] = []

    verify_tasks: list = []
    for food_str in foods:
        line, log_id = await _lookup_and_log(food_str, user_input, date)
        if line.startswith("__needs_grams__"):
            food_name = line.replace("__needs_grams__", "")
            pending_foods.append({"food_str": food_str, "user_input": user_input, "food_name": food_name})
        elif log_id is not None:
            reply_lines.append(line)
            log_ids.append(log_id)
            row = db.get_day_log(date)
            logged = next((r for r in row if r["id"] == log_id), None)
            if logged:
                nutrients = {
                    "calories": logged["calories"], "carbs_g": logged["carbs_g"],
                    "sat_fat_g": logged["sat_fat_g"],
                }
                verify_tasks.append((food_str, logged["grams_eaten"], nutrients))
        else:
            reply_lines.append(line)

    if len(reply_lines) > 1:
        totals = db.get_day_totals(date)
        lim    = config.DAILY_LIMITS
        reply_lines.append(
            f"\nToday: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}"
        )
        await reply_fn("\n".join(reply_lines), reply_markup=_rating_keyboard(log_ids) if log_ids else None)

    for food_name, grams, nutrients in verify_tasks:
        asyncio.create_task(_ai_verify(msg, food_name, grams, nutrients))

    if pending_foods:
        first = pending_foods[0]
        db.set_state(msg.chat.id, "awaiting_grams", {
            "foods": pending_foods, "idx": 0, "log_ids": log_ids, "date": date,
        })
        await reply_fn(f"How many grams of *{first['food_name']}* did you eat?", parse_mode="Markdown")


async def _handle_grams(msg: Message, pending: dict):
    text = msg.text.strip()

    if pending.get("source") == "ocr":
        food_info = pending["foods"][0]
        data      = food_info["result_data"]
        try:
            grams = float(re.search(r"[\d.]+", text).group())
        except (AttributeError, ValueError):
            await msg.reply("Please send a number (grams eaten, or 0 to skip).")
            return
        if grams == 0:
            db.clear_state(msg.chat.id)
            await msg.reply("Skipped.")
            return
        scale     = grams / (data.get("serving_g") or 100)
        nutrients = {
            "calories":  (data.get("calories") or 0) * scale,
            "sat_fat_g": (data.get("sat_fat_g") or 0) * scale,
            "sodium_mg": (data.get("sodium_mg") or 0) * scale,
            "carbs_g":   (data.get("carbs_g") or 0) * scale,
            "sugar_g":   (data.get("sugar_g") or 0) * scale,
        }
        gi_raw = data.get("glycemic_index")
        log_id = db.log_food(
            date=_today(), user_input=food_info["user_input"],
            food_name=food_info["food_str"], source="ocr",
            grams=grams, nutrients=nutrients,
            gi=gi_raw, gi_source="label" if gi_raw else None,
        )
        db.clear_state(msg.chat.id)
        totals = db.get_day_totals(_today())
        lim    = config.DAILY_LIMITS
        serving_g = data.get("serving_g") or 100
        save_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Save to catalog", callback_data=f"cat:save:{log_id}"),
            InlineKeyboardButton(text="— Skip",             callback_data=f"cat:skip:{log_id}"),
        ]])
        await msg.reply(
            f"Logged {food_info['food_str']} ({grams:.0f}g) — {nutrients['calories']:.0f} kcal\n\n"
            f"Today: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}\n\n"
            f"Save this food's nutrition info for future text lookups?",
            reply_markup=save_kb,
        )
        asyncio.create_task(_ai_verify(msg, food_info["food_str"], grams, nutrients))
        return

    try:
        grams = float(re.search(r"[\d.]+", text).group())
    except (AttributeError, ValueError):
        await msg.reply("Please send a number (grams).")
        return

    foods   = pending["foods"]
    idx     = pending["idx"]
    log_ids = pending.get("log_ids", [])
    date    = pending.get("date", _today())
    food    = foods[idx]

    line, log_id = await _lookup_and_log(food["food_str"], food["user_input"], date, grams_override=grams)
    if log_id:
        log_ids.append(log_id)
        row = next((r for r in db.get_day_log(date) if r["id"] == log_id), None)
        if row:
            asyncio.create_task(_ai_verify(msg, food["food_str"], grams, {
                "calories": row["calories"], "carbs_g": row["carbs_g"], "sat_fat_g": row["sat_fat_g"],
            }))

    idx += 1
    if idx < len(foods):
        db.set_state(msg.chat.id, "awaiting_grams", {
            "foods": foods, "idx": idx, "log_ids": log_ids, "date": date,
        })
        next_food = foods[idx]
        await msg.reply(f"{line}\n\nHow many grams of *{next_food['food_name']}* did you eat?",
                        parse_mode="Markdown")
    else:
        db.clear_state(msg.chat.id)
        totals = db.get_day_totals(date)
        lim    = config.DAILY_LIMITS
        await msg.reply(
            f"{line}\n\nToday: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}",
            reply_markup=_rating_keyboard(log_ids) if log_ids else None,
        )


async def _handle_ingredient_change(msg: Message, pending: dict):
    """User typed a new description for an ingredient they want to change."""
    ingredients = pending["ingredients"]
    idx         = pending.get("changing_idx", pending["idx"])
    original    = ingredients[idx]["name"]
    try:
        new_data = await _ai_reestimate_ingredient(original, msg.text.strip())
    except Exception as e:
        await msg.reply(f"Couldn't re-estimate: {e}\n\nTry again or use a button below.")
        return
    # Update Claude estimates in state, run lookup again for new name
    for key in ("name", "grams", "calories", "carbs_g", "sat_fat_g", "sodium_mg", "sugar_g", "fiber_g"):
        if key in new_data:
            target_key = f"claude_{key}" if key != "name" else "name"
            if key == "grams":
                ingredients[idx]["claude_grams"] = new_data[key]
                ingredients[idx]["final_grams"]  = new_data[key]
            elif key == "name":
                ingredients[idx]["name"] = new_data[key]
            else:
                ingredients[idx][f"claude_{key}"] = new_data.get(key, 0)
    # Re-run lookup for updated ingredient
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: nutrition_lookup(new_data.get("name", original))
        )
        per100 = _lookup_result_to_per100g(result)
        if per100:
            ingredients[idx]["lookup_found"]   = True
            ingredients[idx]["lookup_source"]  = result.source
            ingredients[idx]["lookup_name"]    = result.name
            ingredients[idx]["lookup_per_100g"]= per100
        else:
            ingredients[idx]["lookup_found"] = False
    except Exception:
        ingredients[idx]["lookup_found"] = False
    pending["changing_idx"] = idx
    db.set_state(msg.chat.id, "breakdown_review", pending)
    await _show_breakdown_ingredient(msg, pending)


async def _handle_save_recipe_name(msg: Message, pending: dict):
    """User typed a name for saving the recipe."""
    name        = msg.text.strip()
    ingredients = pending.get("ingredients", [])
    total       = pending.get("total", {})
    user_input  = pending.get("user_input", "")
    agreed      = [i for i in ingredients if i.get("status") == "agreed"]
    db.recipe_save(name, user_input, agreed, total)
    db.clear_state(msg.chat.id)
    await msg.reply(f"📝 Recipe *{name}* saved! Use /recipes to see all saved recipes.", parse_mode="Markdown")


def _breakdown_summary_text(pending: dict) -> tuple[str, dict]:
    """Returns (summary_text, total_nutrients) after breakdown is complete."""
    agreed   = [i for i in pending["ingredients"] if i.get("status") == "agreed"]
    skipped  = [i for i in pending["ingredients"] if i.get("status") == "skipped"]
    ai_est   = pending.get("ai_estimate", {})
    date     = pending.get("date", _today())

    total: dict[str, float] = {
        "calories": 0, "carbs_g": 0, "sat_fat_g": 0, "sodium_mg": 0, "sugar_g": 0, "fiber_g": 0
    }
    lines = [f"✅ Logged {len(agreed)}/{len(pending['ingredients'])} ingredient(s)"]
    for ing in agreed:
        grams = ing.get("final_grams") or ing.get("claude_grams") or 0
        if ing.get("lookup_found") and ing.get("lookup_per_100g"):
            n = _scale_nutrients(ing["lookup_per_100g"], grams)
        else:
            n = {
                "calories":  ing.get("claude_calories", 0),
                "carbs_g":   ing.get("claude_carbs_g", 0),
                "sat_fat_g": ing.get("claude_sat_fat_g", 0),
                "sodium_mg": ing.get("claude_sodium_mg", 0),
                "sugar_g":   ing.get("claude_sugar_g", 0),
                "fiber_g":   ing.get("claude_fiber_g", 0),
            }
        for k in total:
            total[k] += n.get(k, 0) or 0
        lines.append(f"  • {ing['name']} — {(n.get('calories') or 0):.0f} kcal")

    if skipped:
        lines.append(f"Skipped: {', '.join(i['name'] for i in skipped)}")

    totals_db = db.get_day_totals(date)
    lim       = config.DAILY_LIMITS
    lines.append(
        f"\nMeal total: {total['calories']:.0f} kcal  •  {total['carbs_g']:.1f}g carbs"
    )

    if ai_est.get("calories"):
        diff = total["calories"] - ai_est["calories"]
        sign = "+" if diff >= 0 else ""
        lines.append(
            f"🤖 AI whole-meal estimate: {ai_est['calories']:.0f} kcal  "
            f"(diff: {sign}{diff:.0f} kcal)"
        )
        if ai_est.get("assumptions"):
            lines.append(f"   _{ai_est['assumptions']}_")

    lines.append(
        f"\nToday: {totals_db['calories']:.0f} / {lim['calories']} kcal  "
        f"{_progress_bar(totals_db['calories'], lim['calories'])}"
    )
    return "\n".join(lines), total


# ---------------------------------------------------------------------------
# Rating callback
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("n:"))
async def handle_rating(cb: CallbackQuery):
    if cb.message.chat.id != config.CHANNEL_ID:
        return
    _, action, ids_str = cb.data.split(":", 2)
    log_ids = [int(i) for i in ids_str.split(",") if i]

    if action == "skip":
        await cb.answer("OK, noted.")
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    liked = action == "like"
    for log_id in log_ids:
        db.update_liked(log_id, liked)

    if liked:
        rows = db.get_day_log(_today())
        for row in rows:
            if row["id"] in log_ids:
                nutrients = {
                    "calories": row["calories"], "sat_fat_g": row["sat_fat_g"],
                    "sodium_mg": row["sodium_mg"], "carbs_g": row["carbs_g"],
                    "sugar_g": row["sugar_g"], "grams_eaten": row["grams_eaten"],
                }
                cooking.add_liked_food(row["food_name"], nutrients, row["glycemic_index"])

    await cb.answer("👍 Got it!" if liked else "👎 Got it!")
    await cb.message.edit_reply_markup(reply_markup=None)


# ---------------------------------------------------------------------------
# Catalog save callback
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("cat:"))
async def handle_catalog(cb: CallbackQuery):
    if cb.message.chat.id != config.CHANNEL_ID:
        return
    _, action, log_id_str = cb.data.split(":", 2)

    if action == "skip":
        await cb.answer("OK.")
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    log_id = int(log_id_str)
    rows   = db.get_day_log(_today())
    row    = next((r for r in rows if r["id"] == log_id), None)

    if row is None:
        await cb.answer("Entry not found.")
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    grams = row.get("grams_eaten") or 100
    scale = 100 / grams
    per_100g = {
        "calories":  (row.get("calories") or 0) * scale,
        "sat_fat_g": (row.get("sat_fat_g") or 0) * scale,
        "sodium_mg": (row.get("sodium_mg") or 0) * scale,
        "carbs_g":   (row.get("carbs_g") or 0) * scale,
        "sugar_g":   (row.get("sugar_g") or 0) * scale,
        "fiber_g":   (row.get("fiber_g") or 0) * scale,
    }
    db.catalog_save(
        name=row["food_name"],
        per_100g=per_100g,
        serving_g=grams,
        source=row.get("source", "ocr"),
    )
    await cb.answer(f"Saved {row['food_name']} to catalog!")
    await cb.message.edit_text(
        cb.message.text.split("\n\nSave this food")[0]
        + f"\n\n📋 Saved to catalog: *{row['food_name']}*",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Comparison choice callback  (cmp:lookup | cmp:ai_est | cmp:breakdown)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("cmp:"))
async def handle_cmp_choice(cb: CallbackQuery):
    if cb.message.chat.id != config.CHANNEL_ID:
        return
    action    = cb.data.split(":")[1]
    state_row = db.get_state(cb.message.chat.id)

    if not state_row or state_row["state"] != "awaiting_cmp_choice":
        await cb.answer("Session expired — please send your food again.")
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    pending = state_row["pending"]
    await cb.message.edit_reply_markup(reply_markup=None)
    db.clear_state(cb.message.chat.id)
    await cb.answer()

    if action == "lookup":
        await _handle_log(
            cb.message, pending["foods"], pending["user_input"],
            reply_fn=cb.message.answer,
        )

    elif action == "ai_est":
        wait = await cb.message.answer("🤖 Estimating...")
        try:
            data = await _ai_estimate_nutrition(pending["user_input"])
        except Exception as e:
            await wait.delete()
            await cb.message.answer(f"AI estimate failed: {e}")
            return
        cal   = data.get("calories") or 0
        carbs = data.get("carbs_g") or 0
        sat   = data.get("sat_fat_g") or 0
        sod   = data.get("sodium_mg") or 0
        sug   = data.get("sugar_g") or 0
        fib   = data.get("fiber_g") or 0
        meal  = data.get("meal_name") or pending["user_input"]
        text  = (
            f"🤖 *AI Estimate: {meal}*\n"
            f"_{data.get('assumptions','')}_\n\n"
            f"  Calories: {cal:.0f} kcal\n"
            f"  Carbs:    {carbs:.1f}g\n"
            f"  Sat fat:  {sat:.1f}g\n"
            f"  Sodium:   {sod:.0f}mg\n"
            f"  Sugar:    {sug:.1f}g\n"
            f"  Fiber:    {fib:.1f}g"
        )
        db.set_state(cb.message.chat.id, "awaiting_ai_log", {
            "user_input": pending["user_input"],
            "meal_name":  meal,
            "date":       pending["date"],
            "nutrients":  {"calories": cal, "carbs_g": carbs, "sat_fat_g": sat,
                           "sodium_mg": sod, "sugar_g": sug, "fiber_g": fib},
        })
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Log it",  callback_data="ai_log:yes"),
            InlineKeyboardButton(text="❌ Discard", callback_data="ai_log:no"),
        ]])
        await wait.delete()
        await cb.message.answer(text, parse_mode="Markdown", reply_markup=kb)

    elif action == "breakdown":
        wait = await cb.message.answer("🔍 Identifying ingredients...")
        try:
            ingredients = await _ai_breakdown_ingredients(pending["user_input"])
            ai_est      = await _ai_estimate_nutrition(pending["user_input"])
        except Exception as e:
            await wait.delete()
            await cb.message.answer(f"AI breakdown failed: {e}")
            return

        # Run our lookup for each ingredient
        for ing in ingredients:
            ing["claude_grams"]    = ing.pop("grams", 0)
            ing["claude_calories"] = ing.pop("calories", 0)
            ing["claude_carbs_g"]  = ing.pop("carbs_g", 0)
            ing["claude_sat_fat_g"]= ing.pop("sat_fat_g", 0)
            ing["claude_sodium_mg"]= ing.pop("sodium_mg", 0)
            ing["claude_sugar_g"]  = ing.pop("sugar_g", 0)
            ing["claude_fiber_g"]  = ing.pop("fiber_g", 0)
            ing["final_grams"]     = ing["claude_grams"]
            ing["status"]          = "pending"
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda n=ing["name"]: nutrition_lookup(n)
                )
                per100 = _lookup_result_to_per100g(result)
                if per100:
                    ing["lookup_found"]    = True
                    ing["lookup_source"]   = result.source
                    ing["lookup_name"]     = result.name
                    ing["lookup_per_100g"] = per100
                else:
                    ing["lookup_found"] = False
            except Exception:
                ing["lookup_found"] = False

        names = [i["name"] for i in ingredients]
        new_pending = {
            "user_input":  pending["user_input"],
            "date":        pending["date"],
            "ai_estimate": ai_est,
            "ingredients": ingredients,
            "idx":         0,
            "log_ids":     [],
        }
        db.set_state(cb.message.chat.id, "breakdown_review", new_pending)
        await wait.edit_text(
            "Found " + str(len(ingredients)) + " ingredient(s):\n"
            + "\n".join(f"  • {n}" for n in names)
            + "\n\nReviewing each one:"
        )
        await _show_breakdown_ingredient(cb.message, new_pending)


# ---------------------------------------------------------------------------
# AI estimate log callback  (ai_log:yes | ai_log:no)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("ai_log:"))
async def handle_ai_log(cb: CallbackQuery):
    if cb.message.chat.id != config.CHANNEL_ID:
        return
    action    = cb.data.split(":")[1]
    state_row = db.get_state(cb.message.chat.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    db.clear_state(cb.message.chat.id)

    if action == "no" or not state_row:
        await cb.answer("Discarded.")
        return

    pending   = state_row["pending"]
    nutrients = pending["nutrients"]
    log_id    = db.log_food(
        date=pending["date"], user_input=pending["user_input"],
        food_name=pending["meal_name"], source="ai_estimate",
        grams=None, nutrients=nutrients, gi=None, gi_source=None,
    )
    totals = db.get_day_totals(pending["date"])
    lim    = config.DAILY_LIMITS
    await cb.answer("Logged!")
    await cb.message.answer(
        f"✅ Logged {pending['meal_name']} — {nutrients['calories']:.0f} kcal\n\n"
        f"Today: {totals['calories']:.0f} / {lim['calories']} kcal  "
        f"{_progress_bar(totals['calories'], lim['calories'])}",
        reply_markup=_rating_keyboard([log_id]),
    )


# ---------------------------------------------------------------------------
# Breakdown ingredient review callback  (bkd:agree | bkd:change | bkd:none)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("bkd:"))
async def handle_breakdown(cb: CallbackQuery):
    if cb.message.chat.id != config.CHANNEL_ID:
        return
    action    = cb.data.split(":")[1]
    state_row = db.get_state(cb.message.chat.id)

    if not state_row or state_row["state"] not in ("breakdown_review", "awaiting_ingredient_change"):
        await cb.answer("Session expired.")
        await cb.message.edit_reply_markup(reply_markup=None)
        return

    pending     = state_row["pending"]
    ingredients = pending["ingredients"]
    idx         = pending.get("changing_idx", pending["idx"])
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()

    if action == "change":
        pending["changing_idx"] = idx
        db.set_state(cb.message.chat.id, "awaiting_ingredient_change", pending)
        await cb.message.answer(
            f"How much *{ingredients[idx]['name']}* did you actually have?\n"
            f"_(e.g. '3 eggs', '150g', '1 large slice')_",
            parse_mode="Markdown",
        )
        return

    if action == "agree":
        ing   = ingredients[idx]
        grams = ing.get("final_grams") or ing.get("claude_grams") or 100
        if ing.get("lookup_found") and ing.get("lookup_per_100g"):
            nutrients = _scale_nutrients(ing["lookup_per_100g"], grams)
            source    = "breakdown_lookup"
        else:
            nutrients = {
                "calories":  ing.get("claude_calories", 0),
                "carbs_g":   ing.get("claude_carbs_g", 0),
                "sat_fat_g": ing.get("claude_sat_fat_g", 0),
                "sodium_mg": ing.get("claude_sodium_mg", 0),
                "sugar_g":   ing.get("claude_sugar_g", 0),
                "fiber_g":   ing.get("claude_fiber_g", 0),
            }
            source = "breakdown_ai"
        log_id = db.log_food(
            date=pending["date"], user_input=pending["user_input"],
            food_name=ing.get("lookup_name") or ing["name"],
            source=source, grams=grams, nutrients=nutrients, gi=None, gi_source=None,
        )
        pending["log_ids"].append(log_id)
        ing["status"] = "agreed"

    elif action == "none":
        ingredients[idx]["status"] = "skipped"

    # Advance to next un-reviewed ingredient
    pending["idx"] = idx + 1
    pending.pop("changing_idx", None)

    if pending["idx"] < len(ingredients):
        db.set_state(cb.message.chat.id, "breakdown_review", pending)
        await _show_breakdown_ingredient(cb.message, pending)
    else:
        # All done
        db.clear_state(cb.message.chat.id)
        summary, total = _breakdown_summary_text(pending)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📝 Save Recipe", callback_data="recipe:save"),
            InlineKeyboardButton(text="— Skip",         callback_data="recipe:skip"),
        ]])
        db.set_state(cb.message.chat.id, "awaiting_recipe_save", {
            "user_input":  pending["user_input"],
            "ingredients": ingredients,
            "total":       total,
        })
        await cb.message.answer(
            summary, parse_mode="Markdown",
            reply_markup=_rating_keyboard(pending["log_ids"]) if pending["log_ids"] else None,
        )
        await cb.message.answer("Save this as a recipe for quick re-logging?", reply_markup=kb)


# ---------------------------------------------------------------------------
# Recipe save callback  (recipe:save | recipe:skip)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("recipe:"))
async def handle_recipe_cb(cb: CallbackQuery):
    if cb.message.chat.id != config.CHANNEL_ID:
        return
    action    = cb.data.split(":")[1]
    state_row = db.get_state(cb.message.chat.id)
    await cb.message.edit_reply_markup(reply_markup=None)

    if action == "skip" or not state_row:
        db.clear_state(cb.message.chat.id)
        await cb.answer("OK.")
        return

    await cb.answer()
    pending = state_row["pending"]
    db.set_state(cb.message.chat.id, "awaiting_recipe_name", pending)
    await cb.message.answer("What should I call this recipe? (e.g. 'Fried eggs with toast')")


# ---------------------------------------------------------------------------
# Recipes command
# ---------------------------------------------------------------------------

@router.message(_CHAN, _THR, Command("recipes"))
async def cmd_recipes(msg: Message):
    recipes = db.recipe_list()
    if not recipes:
        await msg.reply(
            "No saved recipes yet.\n\n"
            "Use the 🔍 Breakdown flow to log a meal and save it as a recipe."
        )
        return
    lines = ["📝 *Saved recipes*"]
    for r in recipes:
        cal = r["total"].get("calories", 0) or 0
        lines.append(f"  • *{r['name']}* — {cal:.0f} kcal")
    lines.append("\nUse /log\\_recipe name to log one.")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("log_recipe"))
async def cmd_log_recipe(msg: Message):
    args = (msg.text or "").split(maxsplit=1)
    name = args[1].strip() if len(args) > 1 else ""
    if not name:
        await msg.reply("Usage: /log\\_recipe name", parse_mode="Markdown")
        return
    recipe = db.recipe_get(name)
    if not recipe:
        await msg.reply(f"Recipe *{name}* not found. Use /recipes to list saved ones.", parse_mode="Markdown")
        return
    date  = _today()
    lines = [f"Logged *{recipe['name']}*:"]
    ids   = []
    for ing in recipe["ingredients"]:
        grams = ing.get("final_grams") or ing.get("claude_grams") or 0
        per100 = ing.get("lookup_per_100g")
        if per100:
            nutrients = _scale_nutrients(per100, grams)
        else:
            nutrients = {k: ing.get(f"claude_{k}", 0) for k in
                         ("calories","carbs_g","sat_fat_g","sodium_mg","sugar_g","fiber_g")}
        log_id = db.log_food(
            date=date, user_input=f"[recipe] {recipe['name']}",
            food_name=ing.get("lookup_name") or ing.get("name", "?"),
            source="recipe", grams=grams, nutrients=nutrients, gi=None, gi_source=None,
        )
        ids.append(log_id)
        lines.append(f"  • {ing.get('name','?')} — {(nutrients.get('calories') or 0):.0f} kcal")
    totals = db.get_day_totals(date)
    lim    = config.DAILY_LIMITS
    lines.append(
        f"\nToday: {totals['calories']:.0f} / {lim['calories']} kcal  "
        f"{_progress_bar(totals['calories'], lim['calories'])}"
    )
    await msg.reply("\n".join(lines), parse_mode="Markdown", reply_markup=_rating_keyboard(ids) if ids else None)
