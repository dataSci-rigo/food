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
# Nutrition lookup + logging
# ---------------------------------------------------------------------------

async def _lookup_and_log(
    food_str: str, user_input: str, date: str, grams_override: float | None = None,
) -> tuple[str, int | None]:
    result = nutrition_lookup(food_str)
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

Just type what you ate — I'll log it.
  "2 eggs and toast"
  "300g chicken breast and rice"

Say "check banana" to look up without logging.
Send a photo of a nutrition label to log from it.
Rate each entry 👍/👎 to update COOKING.md.

/today — today's log + totals
/budget — remaining daily allowance
/history — 7-day calorie summary
"""


@router.message(_CHAN, _THR, Command("help"))
@router.message(_CHAN, _THR, Command("start"))
async def cmd_help(msg: Message):
    await msg.reply(_HELP_TEXT, parse_mode="Markdown")


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
    if state == "awaiting_rating":
        db.clear_state(msg.chat.id)
        await msg.reply("Rating skipped.")

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
        await _handle_log(msg, foods, msg.text)
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


async def _handle_log(msg: Message, foods: list[str], user_input: str):
    date         = _today()
    reply_lines  = ["Logged:"]
    log_ids: list[int]  = []
    pending_foods: list[dict] = []

    for food_str in foods:
        line, log_id = await _lookup_and_log(food_str, user_input, date)
        if line.startswith("__needs_grams__"):
            food_name = line.replace("__needs_grams__", "")
            pending_foods.append({"food_str": food_str, "user_input": user_input, "food_name": food_name})
        elif log_id is not None:
            reply_lines.append(line)
            log_ids.append(log_id)
        else:
            reply_lines.append(line)

    if len(reply_lines) > 1:
        totals = db.get_day_totals(date)
        lim    = config.DAILY_LIMITS
        reply_lines.append(
            f"\nToday: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}"
        )
        await msg.reply("\n".join(reply_lines), reply_markup=_rating_keyboard(log_ids) if log_ids else None)

    if pending_foods:
        first = pending_foods[0]
        db.set_state(msg.chat.id, "awaiting_grams", {
            "foods": pending_foods, "idx": 0, "log_ids": log_ids, "date": date,
        })
        await msg.reply(f"How many grams of *{first['food_name']}* did you eat?", parse_mode="Markdown")


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
        await msg.reply(
            f"Logged {food_info['food_str']} ({grams:.0f}g) — {nutrients['calories']:.0f} kcal\n\n"
            f"Today: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}",
            reply_markup=_rating_keyboard([log_id]),
        )
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
