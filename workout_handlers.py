"""
Workout handlers — exercise logging for thread THREAD_WORKOUT.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

import config
import workout_db

log = logging.getLogger(__name__)

router = Router()
_CHAN  = F.chat.id == config.CHANNEL_ID
_THR   = F.message_thread_id == config.THREAD_WORKOUT


# ---------------------------------------------------------------------------
# Claude exercise extraction
# ---------------------------------------------------------------------------

_WORKOUT_SYSTEM = """\
Extract exercise/workout data from the user's message.
Return ONLY valid JSON with a list of exercises:
{
  "exercises": [
    {
      "name": "push-ups",
      "sets": 3,
      "reps": 10,
      "weight_kg": null,
      "duration_min": null,
      "distance_km": null,
      "notes": null
    }
  ]
}
Use null for fields not mentioned. Convert lbs to kg (1 lb = 0.453592 kg).
Convert miles to km (1 mile = 1.60934 km). No prose, no markdown."""


async def _extract_workout(text: str) -> dict:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=_WORKOUT_SYSTEM,
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


def _format_exercise(ex: dict) -> str:
    parts = [ex["exercise"]]
    if ex.get("sets") and ex.get("reps"):
        parts.append(f"{ex['sets']}×{ex['reps']}")
    elif ex.get("reps"):
        parts.append(f"{ex['reps']} reps")
    if ex.get("weight_kg"):
        parts.append(f"@ {ex['weight_kg']:.1f} kg")
    if ex.get("duration_min"):
        parts.append(f"{ex['duration_min']:.0f} min")
    if ex.get("distance_km"):
        parts.append(f"{ex['distance_km']:.1f} km")
    if ex.get("notes"):
        parts.append(f"({ex['notes']})")
    return "  • " + "  ".join(parts)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
💪 <b>Workout Tracker</b>

Just describe what you did:
  "3 sets of 10 push-ups"
  "Ran 5km in 25 minutes"
  "Bench press: 3x8 @ 185 lbs"
  "20 min cycling, 30 min yoga"

/workout_today — today's log
/history — last 7 days summary
"""


@router.message(_CHAN, _THR, Command("help"))
@router.message(_CHAN, _THR, Command("start"))
async def cmd_help(msg: Message):
    await msg.reply(_HELP_TEXT, parse_mode="HTML")


@router.message(_CHAN, _THR, Command("website"))
async def cmd_website(msg: Message):
    ip = config.VM_TAILSCALE_IP
    if not ip:
        await msg.reply("VM_TAILSCALE_IP not set in .env")
        return
    await msg.reply(f"http://{ip}:9000/workout")


@router.message(_CHAN, _THR, Command("workout_today"))
async def cmd_today(msg: Message):
    date = _today()
    rows = workout_db.get_day_log(date)
    if not rows:
        await msg.reply("No workout logged today. 💤")
        return
    lines = [f"*{date} workout*"]
    for r in rows:
        lines.append(_format_exercise(r))
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("history"))
async def cmd_history(msg: Message):
    rows = workout_db.get_history(7)
    if not rows:
        await msg.reply("No workout history yet.")
        return
    lines = ["*7-day history*"]
    for r in rows:
        lines.append(f"  {r['date']}: {r['exercises']} exercise(s)")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    try:
        data = await _extract_workout(msg.text)
    except Exception as e:
        log.exception("Workout extraction failed")
        await msg.reply(f"Couldn't parse that: {e}")
        return

    exercises = data.get("exercises", [])
    if not exercises:
        await msg.reply("Didn't catch any exercises. Try: \"3 sets of 10 push-ups\" or \"ran 5km\".")
        return

    date  = _today()
    lines = ["Logged:"]
    for ex in exercises:
        workout_db.log_exercise(
            exercise=ex["name"],
            sets=ex.get("sets"),
            reps=ex.get("reps"),
            weight_kg=ex.get("weight_kg"),
            duration_min=ex.get("duration_min"),
            distance_km=ex.get("distance_km"),
            notes=ex.get("notes"),
            raw_input=msg.text,
            date=date,
        )
        parts = [f"• {ex['name']}"]
        if ex.get("sets") and ex.get("reps"):
            parts.append(f"{ex['sets']}×{ex['reps']}")
        if ex.get("weight_kg"):
            parts.append(f"@ {ex['weight_kg']:.1f} kg")
        if ex.get("duration_min"):
            parts.append(f"{ex['duration_min']:.0f} min")
        if ex.get("distance_km"):
            parts.append(f"{ex['distance_km']:.1f} km")
        lines.append("  " + "  ".join(parts))

    await msg.reply("\n".join(lines))
