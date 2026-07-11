#!/usr/bin/env python3
"""
Hub bot — single entry point routing to all services by Telegram topic thread.

  Thread 2 → Nutrition (food logging)
  Thread 3 → Workout   (exercise tracking)
  Thread 4 → Meal prep (fridge inventory)
  Thread 5 → Meds      (medication/supplement tracking)

Channel: https://t.me/c/3592611679
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

import config
import db
import mealprep_db
import workout_db
import meds_db
from nutrition_handlers import router as nutrition_router
from mealprep_handlers import router as mealprep_router
from workout_handlers import router as workout_router
from meds_handlers import router as meds_router

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

bot = Bot(token=config.BOT_TOKEN)
dp  = Dispatcher()

dp.include_router(nutrition_router)
dp.include_router(mealprep_router)
dp.include_router(workout_router)
dp.include_router(meds_router)


async def main():
    db.init_db()
    mealprep_db.init_db()
    workout_db.init_db()
    meds_db.init_db()

    await bot.set_my_commands([
        BotCommand(command="today",         description="Today's food log + totals"),
        BotCommand(command="budget",        description="Remaining daily allowance"),
        BotCommand(command="history",       description="7-day calorie summary"),
        BotCommand(command="fridge",        description="Show fridge contents"),
        BotCommand(command="workout_today", description="Today's workout log"),
        BotCommand(command="meds",          description="Medication catalog (tap to log)"),
        BotCommand(command="med_log",       description="Today's medication doses"),
        BotCommand(command="supps",         description="Supplement catalog (tap to log)"),
        BotCommand(command="supp_log",      description="Today's supplement doses"),
        BotCommand(command="help",          description="Show help for current topic"),
    ])

    log.info("Hub bot starting (channel=%s)...", config.CHANNEL_ID)
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
