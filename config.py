import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    _HERE = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_HERE, ".env"))
    # Also load parent .env so shared keys (ANTHROPIC, USDA, etc.) are available
    load_dotenv(os.path.join(_HERE, "..", ".env"))
except ImportError:
    pass

BOT_TOKEN     = os.environ["food_bot"]
OWNER_CHAT_ID = int(str(os.environ.get("OWNER_CHAT_ID", "0")).strip("'\""))
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
TZ            = ZoneInfo("America/Los_Angeles")
DB_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "food.db")
COOKING_MD    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "COOKING.md")

DAILY_LIMITS = {
    "calories":  2000,
    "sat_fat_g": 20,
    "sodium_mg": 2300,
    "carbs_g":   275,
    "sugar_g":   50,
}
