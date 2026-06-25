"""
Food tracking Telegram bot (aiogram 3).

Flow:
  1. User sends free text about food eaten.
  2. Claude (haiku) extracts intent + food list.
  3. For each food: nutrition.lookup() → if per_100g, ask grams.
  4. GI looked up in parallel via gi.lookup_gi().
  5. Logged to SQLite. Rating keyboard sent.
  6. 👍/👎 updates liked flag and COOKING.md.

Commands:
  /start   — welcome
  /today   — today's log
  /budget  — remaining daily allowance
  /history — 7-day summary
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
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

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _progress_bar(actual: float, limit: float, width: int = 10) -> str:
    pct = min(actual / limit, 1.0) if limit else 0
    filled = round(pct * width)
    bar = "▓" * filled + "░" * (width - filled)
    return f"{bar}  {pct*100:.0f}%"


def _format_totals(totals: dict) -> str:
    lim = config.DAILY_LIMITS
    lines = [
        f"Calories:  {totals['calories']:.0f} / {lim['calories']} kcal  "
        f"{_progress_bar(totals['calories'], lim['calories'])}",
        f"Sat fat:   {totals['sat_fat_g']:.1f} / {lim['sat_fat_g']} g",
        f"Sodium:    {totals['sodium_mg']:.0f} / {lim['sodium_mg']} mg",
        f"Carbs:     {totals['carbs_g']:.0f} / {lim['carbs_g']} g",
        f"Sugar:     {totals['sugar_g']:.0f} / {lim['sugar_g']} g",
    ]
    return "\n".join(lines)


def _format_budget(totals: dict) -> str:
    lim = config.DAILY_LIMITS
    def rem(key, unit, decimals=0):
        left = lim[key] - totals.get(key, 0)
        fmt = f"{left:.{decimals}f}"
        flag = " ⚠️" if left < 0 else ""
        return f"{fmt} {unit}{flag}"

    lines = [
        "Remaining budget:",
        f"  Calories:  {rem('calories', 'kcal')}",
        f"  Sat fat:   {rem('sat_fat_g', 'g', 1)}",
        f"  Sodium:    {rem('sodium_mg', 'mg')}",
        f"  Carbs:     {rem('carbs_g', 'g')}",
        f"  Sugar:     {rem('sugar_g', 'g')}",
    ]
    return "\n".join(lines)


def _rating_keyboard(log_ids: list[int]) -> InlineKeyboardMarkup:
    ids_str = ",".join(str(i) for i in log_ids)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Liked it",    callback_data=f"rate:like:{ids_str}"),
        InlineKeyboardButton(text="👎 Didn't like", callback_data=f"rate:dislike:{ids_str}"),
        InlineKeyboardButton(text="— Skip",         callback_data=f"rate:skip:{ids_str}"),
    ]])


def _owner_only(chat_id: int) -> bool:
    return chat_id == config.OWNER_CHAT_ID


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
- "check": user wants nutrition info without logging (e.g. "check banana")
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
# Nutrition lookup + logging for a single food item
# ---------------------------------------------------------------------------

async def _lookup_and_log(
    food_str: str,
    user_input: str,
    date: str,
    grams_override: float | None = None,
) -> tuple[str, int | None]:
    """
    Returns (reply_line, log_id_or_None).
    log_id is None if the food couldn't be found.
    """
    result = nutrition_lookup(food_str)
    if result is None:
        return f"• {food_str} — couldn't find nutrition data", None

    # Determine grams and scale nutrients
    grams = grams_override
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
            # Caller must handle asking for grams
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

    # GI lookup (non-blocking); pass raw per-unit macros for ratio estimation
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
        date=date,
        user_input=user_input,
        food_name=result.name,
        source=result.source,
        grams=grams,
        nutrients=nutrients,
        gi=gi_val,
        gi_source=gi_src,
    )

    gi_str = f"GI {gi_val:.0f}" if gi_val is not None else "GI —"
    gram_str = f" ({grams:.0f}g)" if grams else ""
    cal = nutrients.get("calories", 0)
    sat = nutrients.get("sat_fat_g", 0)
    sod = nutrients.get("sodium_mg", 0)
    line = (f"• {result.name}{gram_str} — "
            f"{cal:.0f} kcal, {gi_str}, "
            f"{sat:.1f}g sat fat, {sod:.0f}mg sodium")
    return line, log_id


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not _owner_only(msg.chat.id):
        return
    await msg.answer(
        f"Food tracker ready!\n"
        f"Your chat id: {msg.chat.id}\n\n"
        "Just tell me what you ate. Commands:\n"
        "/today — today's log\n"
        "/budget — remaining allowance\n"
        "/history — 7-day summary"
    )


@dp.message(Command("today"))
async def cmd_today(msg: Message):
    if not _owner_only(msg.chat.id):
        return
    date = _today()
    rows = db.get_day_log(date)
    if not rows:
        await msg.answer("Nothing logged today.")
        return
    lines = [f"*{date}*"]
    for r in rows:
        gi_str = f"GI {r['glycemic_index']:.0f}" if r["glycemic_index"] is not None else "GI —"
        liked = " 👍" if r["liked"] == 1 else (" 👎" if r["liked"] == 0 else "")
        time_str = r["logged_at"][11:16] if len(r["logged_at"]) > 15 else ""
        lines.append(
            f"  {time_str} {r['food_name']} — "
            f"{(r['calories'] or 0):.0f} kcal, {gi_str}{liked}"
        )
    totals = db.get_day_totals(date)
    lines.append("")
    lines.append(_format_totals(totals))
    await msg.answer("\n".join(lines), parse_mode="Markdown")


@dp.message(Command("budget"))
async def cmd_budget(msg: Message):
    if not _owner_only(msg.chat.id):
        return
    totals = db.get_day_totals(_today())
    await msg.answer(_format_budget(totals))


@dp.message(Command("history"))
async def cmd_history(msg: Message):
    if not _owner_only(msg.chat.id):
        return
    rows = db.get_history_totals(7)
    if not rows:
        await msg.answer("No history yet.")
        return
    lines = ["*7-day history*"]
    for r in rows:
        pct = int(r["calories"] / config.DAILY_LIMITS["calories"] * 100)
        lines.append(f"  {r['date']}: {r['calories']:.0f} kcal ({pct}%)")
    await msg.answer("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Photo handler (OCR)
# ---------------------------------------------------------------------------

@dp.message(F.photo)
async def handle_photo(msg: Message):
    if not _owner_only(msg.chat.id):
        return
    await msg.answer("Reading label...")
    try:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
        bio = await bot.download_file(file.file_path)
        image_bytes = bio.read()
        data = await ocr.read_label(image_bytes)
    except ValueError as e:
        await msg.answer(f"Couldn't read a nutrition label: {e}")
        return
    except Exception as e:
        log.exception("OCR error")
        await msg.answer(f"Error reading label: {e}")
        return

    food_name = data.get("food_name") or "Unknown food"
    serving_g = data.get("serving_g")

    summary_lines = [f"*{food_name}*"]
    for key, label in [("calories","Calories"), ("carbs_g","Carbs"),
                       ("sat_fat_g","Sat fat"), ("sodium_mg","Sodium"),
                       ("sugar_g","Sugar"), ("glycemic_index","GI")]:
        val = data.get(key)
        if val is not None:
            summary_lines.append(f"  {label}: {val}")
    if serving_g:
        summary_lines.append(f"  Serving: {serving_g}g")

    summary_lines.append("\nHow many grams did you eat? (or 0 to skip logging)")

    # Save OCR data for next reply
    db.set_state(msg.chat.id, "awaiting_grams", {
        "source": "ocr",
        "foods": [{
            "food_str": food_name,
            "user_input": f"[label] {food_name}",
            "result_data": data,
        }],
        "idx": 0,
        "log_ids": [],
    })
    await msg.answer("\n".join(summary_lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Free text handler
# ---------------------------------------------------------------------------

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    if not _owner_only(msg.chat.id):
        return

    state_row = db.get_state(msg.chat.id)
    state = state_row["state"] if state_row else "idle"

    if state == "awaiting_grams":
        await _handle_grams(msg, state_row["pending"])
        return
    if state == "awaiting_rating":
        db.clear_state(msg.chat.id)
        await msg.answer("Rating skipped.")

    # Intent extraction
    try:
        intent_data = await _extract_intent(msg.text)
    except Exception as e:
        log.exception("Intent extraction failed")
        await msg.answer(f"Sorry, I couldn't understand that: {e}")
        return

    intent = intent_data.get("intent", "other")
    foods = intent_data.get("foods", [])

    if intent_data.get("clarification_needed"):
        q = intent_data.get("clarification_question", "Could you clarify?")
        db.set_state(msg.chat.id, "clarifying", {"original": msg.text, "question": q})
        await msg.answer(q)
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
        await msg.answer("I track food! Tell me what you ate, or ask /today or /budget.")


async def _handle_check(msg: Message, foods: list[str]):
    if not foods:
        await msg.answer("What food do you want to check?")
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
                result.name,
                carbs_g=result.carbs_g,
                sugar_g=result.sugar_g,
                fiber_g=result.fiber_g,
            ),
        )
        gi_str = f"GI {gi_val:.0f}" if gi_val is not None else "GI —"
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
    await msg.answer("\n".join(lines), parse_mode="Markdown")


async def _handle_log(msg: Message, foods: list[str], user_input: str):
    date = _today()
    reply_lines = ["Logged:"]
    log_ids: list[int] = []
    pending_foods: list[dict] = []  # foods that still need gram input

    for food_str in foods:
        line, log_id = await _lookup_and_log(food_str, user_input, date)
        if line.startswith("__needs_grams__"):
            food_name = line.replace("__needs_grams__", "")
            pending_foods.append({"food_str": food_str, "user_input": user_input,
                                   "food_name": food_name})
        elif log_id is not None:
            reply_lines.append(line)
            log_ids.append(log_id)
        else:
            reply_lines.append(line)

    # Show logged items so far
    if len(reply_lines) > 1:
        totals = db.get_day_totals(date)
        lim = config.DAILY_LIMITS
        reply_lines.append(
            f"\nToday: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}"
        )
        keyboard = _rating_keyboard(log_ids) if log_ids else None
        await msg.answer("\n".join(reply_lines), parse_mode=None,
                         reply_markup=keyboard)

    # Ask for grams if any per_100g foods remain
    if pending_foods:
        first = pending_foods[0]
        db.set_state(msg.chat.id, "awaiting_grams", {
            "foods": pending_foods,
            "idx": 0,
            "log_ids": log_ids,
            "date": date,
        })
        await msg.answer(f"How many grams of *{first['food_name']}* did you eat?",
                         parse_mode="Markdown")


async def _handle_grams(msg: Message, pending: dict):
    text = msg.text.strip()

    # Handle OCR flow
    if pending.get("source") == "ocr":
        food_info = pending["foods"][0]
        data = food_info["result_data"]
        grams_text = text
        try:
            grams = float(re.search(r"[\d.]+", grams_text).group())
        except (AttributeError, ValueError):
            await msg.answer("Please send a number (grams eaten, or 0 to skip).")
            return
        if grams == 0:
            db.clear_state(msg.chat.id)
            await msg.answer("Skipped.")
            return
        scale = grams / (data.get("serving_g") or 100)
        nutrients = {
            "calories":  (data.get("calories") or 0) * scale,
            "sat_fat_g": (data.get("sat_fat_g") or 0) * scale,
            "sodium_mg": (data.get("sodium_mg") or 0) * scale,
            "carbs_g":   (data.get("carbs_g") or 0) * scale,
            "sugar_g":   (data.get("sugar_g") or 0) * scale,
        }
        gi_raw = data.get("glycemic_index")
        log_id = db.log_food(
            date=_today(),
            user_input=food_info["user_input"],
            food_name=food_info["food_str"],
            source="ocr",
            grams=grams,
            nutrients=nutrients,
            gi=gi_raw,
            gi_source="label" if gi_raw else None,
        )
        db.clear_state(msg.chat.id)
        totals = db.get_day_totals(_today())
        lim = config.DAILY_LIMITS
        reply = (
            f"Logged {food_info['food_str']} ({grams:.0f}g) — "
            f"{nutrients['calories']:.0f} kcal\n\n"
            f"Today: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}"
        )
        await msg.answer(reply, reply_markup=_rating_keyboard([log_id]))
        return

    # Normal per_100g flow
    try:
        grams = float(re.search(r"[\d.]+", text).group())
    except (AttributeError, ValueError):
        await msg.answer("Please send a number (grams).")
        return

    foods = pending["foods"]
    idx = pending["idx"]
    log_ids = pending.get("log_ids", [])
    date = pending.get("date", _today())
    food = foods[idx]

    line, log_id = await _lookup_and_log(
        food["food_str"], food["user_input"], date, grams_override=grams
    )
    if log_id:
        log_ids.append(log_id)

    idx += 1
    if idx < len(foods):
        # More foods awaiting grams
        db.set_state(msg.chat.id, "awaiting_grams", {
            "foods": foods, "idx": idx, "log_ids": log_ids, "date": date
        })
        next_food = foods[idx]
        await msg.answer(f"{line}\n\nHow many grams of *{next_food['food_name']}* did you eat?",
                         parse_mode="Markdown")
    else:
        db.clear_state(msg.chat.id)
        totals = db.get_day_totals(date)
        lim = config.DAILY_LIMITS
        reply = (
            f"{line}\n\n"
            f"Today: {totals['calories']:.0f} / {lim['calories']} kcal  "
            f"{_progress_bar(totals['calories'], lim['calories'])}"
        )
        keyboard = _rating_keyboard(log_ids) if log_ids else None
        await msg.answer(reply, parse_mode=None, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Rating callback
# ---------------------------------------------------------------------------

@dp.callback_query(F.data.startswith("rate:"))
async def handle_rating(cb: CallbackQuery):
    if not _owner_only(cb.message.chat.id):
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
        # Update COOKING.md for each liked item
        rows = db.get_day_log(_today())
        for row in rows:
            if row["id"] in log_ids:
                nutrients = {
                    "calories":  row["calories"],
                    "sat_fat_g": row["sat_fat_g"],
                    "sodium_mg": row["sodium_mg"],
                    "carbs_g":   row["carbs_g"],
                    "sugar_g":   row["sugar_g"],
                    "grams_eaten": row["grams_eaten"],
                }
                cooking.add_liked_food(row["food_name"], nutrients, row["glycemic_index"])

    emoji = "👍" if liked else "👎"
    await cb.answer(f"{emoji} Got it!")
    await cb.message.edit_reply_markup(reply_markup=None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    db.init_db()
    log.info("Starting food bot (polling)...")
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
