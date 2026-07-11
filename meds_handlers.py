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


def _fmt_time(utc_str: str) -> str:
    from datetime import timezone
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(config.TZ).strftime("%H:%M")
    except Exception:
        return utc_str[11:16] if len(utc_str) > 15 else utc_str


def _catalog_keyboard(category: str | None = None) -> InlineKeyboardMarkup | None:
    catalog = meds_db.get_catalog(active_only=True)
    if category:
        catalog = [m for m in catalog if m["category"] == category]
    if not catalog:
        return None
    buttons = []
    for med in catalog:
        dose_label = ""
        if med["dose_amount"] and med["dose_unit"]:
            dose_label = f" {med['dose_amount']:.4g}{med['dose_unit']}"
        label = f"{med['name']}{dose_label}"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"med:take:{med['name']}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _format_catalog(category: str | None = None) -> str:
    catalog = meds_db.get_catalog(active_only=True)
    if category:
        catalog = [m for m in catalog if m["category"] == category]
    if not catalog:
        label = "medications" if category == "medication" else "supplements"
        return f"No {label} in catalog yet."
    lines = ["💊 *Medications*" if category == "medication" else "🌿 *Supplements*"]
    for m in catalog:
        dose = f" — {m['dose_amount']:.4g} {m['dose_unit']}" if m["dose_amount"] else ""
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

<b>Medications</b>
/meds — catalog with tap-to-log buttons
/med_log — today's medication doses
/add_med name [dose] [unit]
  e.g. /add_med Metformin 500 mg

<b>Supplements</b>
/supps — catalog with tap-to-log buttons
/supp_log — today's supplement doses
/add_supp name [dose] [unit]
  e.g. /add_supp "Vitamin D" 2000 IU

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


# ---- Medications ----

@router.message(_CHAN, _THR, Command("meds"))
async def cmd_meds(msg: Message):
    keyboard = _catalog_keyboard(category="medication")
    if keyboard is None:
        await msg.reply("No medications in catalog. Use /add_med name dose unit")
        return
    await msg.reply(_format_catalog(category="medication"), parse_mode="Markdown", reply_markup=keyboard)


@router.message(_CHAN, _THR, Command("med_log"))
async def cmd_med_log(msg: Message):
    doses = meds_db.get_today_doses(category="medication")
    if not doses:
        await msg.reply("No medications logged today.")
        return
    lines = ["*Medications taken today*"]
    for d in doses:
        time_str = _fmt_time(d["logged_at"])
        dose_str = f" {d['dose_amount']:.4g} {d['dose_unit']}" if d["dose_amount"] else ""
        lines.append(f"  {time_str} {d['med_name']}{dose_str}")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("add_med"))
async def cmd_add_med(msg: Message):
    args = (msg.text or "").split(maxsplit=1)[1] if len((msg.text or "").split()) > 1 else ""
    if not args:
        await msg.reply("Usage: /add_med name [dose] [unit]\nExample: /add_med Metformin 500 mg")
        return
    name, dose_amount, dose_unit, _ = _parse_add_med(args)
    if not name:
        await msg.reply("Please provide a medication name.")
        return
    meds_db.add_med(name, dose_amount, dose_unit, category="medication")
    dose_str = f" {dose_amount:.4g} {dose_unit}" if dose_amount else ""
    await msg.reply(
        f"✅ Added *{name}*{dose_str} to medications.\n\nUse /meds to see your catalog.",
        parse_mode="Markdown",
    )


# ---- Supplements ----

@router.message(_CHAN, _THR, Command("supps"))
async def cmd_supps(msg: Message):
    keyboard = _catalog_keyboard(category="supplement")
    if keyboard is None:
        await msg.reply("No supplements in catalog. Use /add_supp name dose unit")
        return
    await msg.reply(_format_catalog(category="supplement"), parse_mode="Markdown", reply_markup=keyboard)


@router.message(_CHAN, _THR, Command("supp_log"))
async def cmd_supp_log(msg: Message):
    doses = meds_db.get_today_doses(category="supplement")
    if not doses:
        await msg.reply("No supplements logged today.")
        return
    lines = ["*Supplements taken today*"]
    for d in doses:
        time_str = _fmt_time(d["logged_at"])
        dose_str = f" {d['dose_amount']:.4g} {d['dose_unit']}" if d["dose_amount"] else ""
        lines.append(f"  {time_str} {d['med_name']}{dose_str}")
    await msg.reply("\n".join(lines), parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("add_supp"))
async def cmd_add_supp(msg: Message):
    args = (msg.text or "").split(maxsplit=1)[1] if len((msg.text or "").split()) > 1 else ""
    if not args:
        await msg.reply('Usage: /add_supp name [dose] [unit]\nExample: /add_supp "Vitamin D" 2000 IU')
        return
    name, dose_amount, dose_unit, _ = _parse_add_med(args)
    if not name:
        await msg.reply("Please provide a supplement name.")
        return
    meds_db.add_med(name, dose_amount, dose_unit, category="supplement")
    dose_str = f" {dose_amount:.4g} {dose_unit}" if dose_amount else ""
    await msg.reply(
        f"✅ Added *{name}*{dose_str} to supplements.\n\nUse /supps to see your catalog.",
        parse_mode="Markdown",
    )


# ---- Shared remove ----

@router.message(_CHAN, _THR, Command("remove_med"))
async def cmd_remove_med(msg: Message):
    args = (msg.text or "").split(maxsplit=1)
    name = args[1].strip() if len(args) > 1 else ""
    if not name:
        await msg.reply("Usage: /remove_med name")
        return
    removed = meds_db.remove_med(name)
    if removed:
        await msg.reply(f"Removed *{name}* from catalog.", parse_mode="Markdown")
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
