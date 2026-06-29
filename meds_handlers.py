"""
Meds handlers — medication/supplement tracking for thread THREAD_MEDS.
Catalog shown as an inline keyboard; tap to log dose.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import meds_db

log = logging.getLogger(__name__)

router = Router()
_CHAN  = F.chat.id == config.CHANNEL_ID
_THR   = F.message_thread_id == config.THREAD_MEDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _catalog_keyboard() -> InlineKeyboardMarkup | None:
    catalog = meds_db.get_catalog(active_only=True)
    if not catalog:
        return None
    buttons = []
    for med in catalog:
        dose_label = ""
        if med["dose_amount"] and med["dose_unit"]:
            dose_label = f" {med['dose_amount']:.0f}{med['dose_unit']}"
        label = f"{med['name']}{dose_label}"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"med:take:{med['name']}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _format_catalog() -> str:
    catalog = meds_db.get_catalog(active_only=True)
    if not catalog:
        return "No medications or supplements in catalog. Use /add_med to add one."
    by_cat: dict[str, list] = {}
    for med in catalog:
        by_cat.setdefault(med["category"], []).append(med)
    lines = ["💊 *Med / Supplement Catalog*"]
    for cat, meds in by_cat.items():
        lines.append(f"\n_{cat.title()}_")
        for m in meds:
            dose = f" — {m['dose_amount']:.0f} {m['dose_unit']}" if m["dose_amount"] else ""
            note = f"  _{m['notes']}_" if m["notes"] else ""
            lines.append(f"  • {m['name']}{dose}{note}")
    return "\n".join(lines)


def _parse_add_med(text: str) -> tuple[str, float | None, str | None, str]:
    """Parse '/add_med name dose unit [category]' or '/add_med name'."""
    parts = text.strip().split()
    if not parts:
        return "", None, None, "supplement"
    name = parts[0]
    dose_amount = None
    dose_unit   = None
    category    = "supplement"
    if len(parts) >= 2:
        try:
            dose_amount = float(parts[1])
        except ValueError:
            pass
    if len(parts) >= 3:
        dose_unit = parts[2]
    if len(parts) >= 4:
        category = parts[3].lower()
    return name, dose_amount, dose_unit, category


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
💊 <b>Meds &amp; Supplements</b>

Tap a button below to log a dose, or use commands:

/meds — show catalog with tap-to-log buttons
/med_log — today's doses taken
/add_med name [dose] [unit] [category]
  e.g. /add_med Metformin 500 mg medication
  e.g. /add_med "Vitamin D" 2000 IU supplement
/remove_med name — deactivate from catalog
"""


@router.message(_CHAN, _THR, Command("help"))
@router.message(_CHAN, _THR, Command("start"))
async def cmd_help(msg: Message):
    keyboard = _catalog_keyboard()
    await msg.reply(_HELP_TEXT, parse_mode="HTML", reply_markup=keyboard)


@router.message(_CHAN, _THR, Command("website"))
async def cmd_website(msg: Message):
    ip = config.VM_TAILSCALE_IP
    if not ip:
        await msg.reply("VM_TAILSCALE_IP not set in .env")
        return
    await msg.reply(f"http://{ip}:9000/meds")


@router.message(_CHAN, _THR, Command("meds"))
async def cmd_meds(msg: Message):
    keyboard = _catalog_keyboard()
    if keyboard is None:
        await msg.reply("No meds in catalog. Use /add_med to add one.")
        return
    await msg.reply(_format_catalog(), parse_mode="Markdown", reply_markup=keyboard)


@router.message(_CHAN, _THR, Command("med_log"))
async def cmd_med_log(msg: Message):
    doses = meds_db.get_today_doses()
    if not doses:
        await msg.reply("No doses logged today.")
        return
    lines = [f"*Doses taken today*"]
    for d in doses:
        time_str  = d["logged_at"][11:16]
        dose_str  = f" {d['dose_amount']:.0f} {d['dose_unit']}" if d["dose_amount"] else ""
        lines.append(f"  {time_str} {d['med_name']}{dose_str}")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("add_med"))
async def cmd_add_med(msg: Message):
    args = (msg.text or "").split(maxsplit=1)[1] if len((msg.text or "").split()) > 1 else ""
    if not args:
        await msg.reply("Usage: /add_med name [dose] [unit] [category]\nExample: /add_med Metformin 500 mg medication")
        return
    name, dose_amount, dose_unit, category = _parse_add_med(args)
    if not name:
        await msg.reply("Please provide a medication name.")
        return
    meds_db.add_med(name, dose_amount, dose_unit, category)
    dose_str = f" {dose_amount:.0f} {dose_unit}" if dose_amount else ""
    await msg.reply(
        f"✅ Added *{name}*{dose_str} to catalog as _{category}_.\n\nUse /meds to see your catalog.",
        parse_mode="Markdown",
    )


@router.message(_CHAN, _THR, Command("remove_med"))
async def cmd_remove_med(msg: Message):
    args = (msg.text or "").split(maxsplit=1)
    name = args[1].strip() if len(args) > 1 else ""
    if not name:
        await msg.reply("Usage: /remove_med name")
        return
    removed = meds_db.remove_med(name)
    if removed:
        await msg.reply(f"Removed *{name}* from active catalog.", parse_mode="Markdown")
    else:
        await msg.reply(f"*{name}* not found in catalog.", parse_mode="Markdown")


@router.message(_CHAN, _THR, F.text & ~F.text.startswith("/"))
async def handle_text(msg: Message):
    await msg.reply(
        "Use /meds to see your catalog and tap to log a dose.\n"
        "Or /add_med to add a new medication/supplement."
    )


# ---------------------------------------------------------------------------
# Callback: tap med button to log dose
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("med:take:"))
async def handle_take(cb: CallbackQuery):
    if cb.message.chat.id != config.CHANNEL_ID:
        return
    med_name = cb.data.split(":", 2)[2]
    meds_db.log_dose(med_name)
    time_str = datetime.now(config.TZ).strftime("%H:%M")
    await cb.answer(f"✅ Logged {med_name} at {time_str}")
    # Refresh the keyboard so the user can quickly tap another
    keyboard = _catalog_keyboard()
    try:
        await cb.message.edit_reply_markup(reply_markup=keyboard)
    except Exception:
        pass
