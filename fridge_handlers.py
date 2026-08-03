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

Recipe ids are strings everywhere in this module: plain digits ("42") mean
a corpus recipe (fridge_recipes/data/recipes.db), a "c" prefix ("c42") means
an unpromoted custom recipe ingested via photo/paste (app.db.custom_recipes,
core/custom_recipes.py) -- see etl/promote_custom_recipes.py for how those
eventually get folded into the corpus (with real nutrition + embeddings) on
a laptop-run batch, not live here. /cook and /find merge both sources.

fridge_recipes is installed as an editable dependency of this project (see
requirements.txt: `-e ../fridge-recipes`) so its core/ modules import
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
import db as nutrition_db
import gi as gi_mod
from nutrition import lookup as nutrition_lookup
from recipe_ingest import extract_from_image, extract_from_text

from core.db import connect as fridge_connect
from core.config import APP_DB_PATH, RECIPES_DB_PATH
from core import custom_recipes as custom_recipes_db
from core import inventory as fridge_inventory
from core import substitutions as substitutions_db
from core.models import InventoryItem, RankFilters
from core.normalize import canonicalize
from core.nutrition import lookup as ingredient_nutrition_lookup
from core.rank import rank_recipes
from core.recommend import recommend as recommend_similar
from core.search import get_recipe, load_candidate_recipes, search_recipes

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
  "intent": "add" | "eat" | "remove" | "show" | "cook" | "find" | "substitute" | "ingest" | "other",
  "items": [
    {"name": "chicken breast", "quantity": 500, "unit": "g"}
  ],
  "filters": {"max_active_min": null, "max_total_min": null, "category": null, "ingredients": []}
}
- "add"   : user bought or stocked food (push to fridge)
- "eat"   : user consumed something from the fridge (reduce fridge AND log to nutrition)
- "remove": user discarded food without eating (spoiled, gave away)
- "show"  : user wants to see fridge contents
- "cook"  : user is asking what they can make from their CURRENT fridge contents in
            general (e.g. "what can I make?", "any ideas for dinner?") -- no specific
            dish named
- "find"  : user names a dish, or describes recipe criteria, and wants matching
            recipes (e.g. "what do I need to make hummus?", "a quick dessert under
            20 minutes", "dinner with chicken and garlic"). Put a bare dish/search
            name in items[0].name if there is one (else omit items). Fill "filters"
            with any of: max_active_min/max_total_min (minutes, from phrases like
            "under 30 minutes" or "quick"->30), category (one of: breakfast,
            beverage, dessert, bread, soup, salad, appetizer, sauce_or_condiment,
            side_dish -- only if clearly implied), ingredients (canonical-ish
            ingredient names explicitly mentioned as required). Omit/null anything
            not mentioned.
- "substitute": user asks what they can use instead of a specific ingredient
            (e.g. "what can I substitute for buttermilk?"). items[0].name = that
            ingredient.
- "ingest": the message itself IS a recipe the user pasted (a title, a list of
            ingredients, and/or cooking steps) that they want saved -- not a short
            fridge command. items and filters not needed.
- "other" : unrelated
For "eat" without explicit quantity, use a reasonable serving size and note it.
Use "g" for solids, "ml" for liquids, "count" for items (eggs, apples, etc.).
No prose, no markdown."""


async def _extract_intent(text: str) -> dict:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=_INTENT_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Recipe id routing: "42" = corpus, "c42" = custom (unpromoted)
# ---------------------------------------------------------------------------

def _parse_ref(ref: str) -> tuple[str, int] | None:
    ref = ref.strip()
    if ref[:1].lower() == "c" and ref[1:].isdigit():
        return ("custom", int(ref[1:]))
    if ref.isdigit():
        return ("corpus", int(ref))
    return None


def _display_id(source: str, recipe_id: int) -> str:
    return f"c{recipe_id}" if source == "custom" else str(recipe_id)


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


def _get_substitutes_fn():
    return lambda name: substitutions_db.get_substitutes(name, db_path=APP_DB_PATH)


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
  "What do I need to make hummus?" — look up a specific dish
  "A quick dessert under 20 minutes" — filtered search
  "What can I substitute for buttermilk?" — substitution ideas
  Paste a recipe (title + ingredients) to save it

/fridge — show fridge contents
/cook — top recipe suggestions from what's in your fridge
/find <name> [active<30] [type:dessert] [has:garlic,lemon] — search
/recipe <id> — full recipe detail (ingredients you have/miss, directions);
  use a "c" prefix (e.g. /recipe c12) for a recipe you ingested
/save <id> [stars] — bookmark a corpus recipe you liked (default 5★)
/recommend — recipes similar to the ones you've saved
/sub <ingredient> — what can I use instead?
/deleterecipe c<id> — remove something you ingested by mistake
Send a photo or screenshot of a recipe to ingest it.

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
    subs_fn = _get_substitutes_fn()

    recipes = load_candidate_recipes(names, db_path=RECIPES_DB_PATH)
    corpus_results = rank_recipes(
        recipes, inv, filters=RankFilters(), top_n=top_n, get_substitutes_fn=subs_fn
    )
    custom_results = custom_recipes_db.rank_custom(
        inv, top_n=top_n, get_substitutes_fn=subs_fn, db_path=APP_DB_PATH
    )

    combined = (
        [("corpus", r) for r in corpus_results] + [("custom", r) for r in custom_results]
    )
    combined.sort(key=lambda pair: pair[1].score, reverse=True)
    combined = combined[:top_n]

    if not combined:
        return "Couldn't find a recipe using what's in your fridge right now."
    lines = ["🍳 *Top picks from your fridge:*"]
    for source, r in combined:
        did = _display_id(source, r.recipe.id)
        time_str = f", {r.recipe.active_min}m active" if r.recipe.active_min else ""
        lines.append(f"\n*#{did}* {r.recipe.title}{time_str}\n  {r.explanation}")
    lines.append("\nTap /recipe <id> for the full recipe.")
    return "\n".join(lines)


@router.message(_CHAN, _THR, Command("cook"))
async def cmd_cook(msg: Message):
    reply = await asyncio.get_event_loop().run_in_executor(None, _cook_reply)
    await msg.reply(reply, parse_mode="Markdown")


def _recipe_detail_text(ref: str) -> str | None:
    """Shared renderer for /recipe <id>, the "find" intent's single-match
    case, and the pick-one menu's callback handler -- one format everywhere.
    `ref` is a display id: "42" (corpus) or "c42" (custom, unpromoted)."""
    parsed = _parse_ref(ref)
    if parsed is None:
        return None
    source, recipe_id = parsed

    have_names = {
        i["canonical_ingredient"]
        for i in fridge_inventory.list_inventory(db_path=APP_DB_PATH)
    }

    if source == "corpus":
        detail = get_recipe(recipe_id, db_path=RECIPES_DB_PATH)
        if detail is None:
            return None
        title, directions = detail["title"], detail["directions"]
        ingredient_rows = [(i["name"], i["quantity_text"]) for i in detail["ingredients"]]
    else:
        detail = custom_recipes_db.get_custom_recipe(recipe_id, db_path=APP_DB_PATH)
        if detail is None:
            return None
        title, directions = detail["name"], detail["directions"]
        ingredient_rows = [
            (canonicalize(i["canonical_name"]), i.get("quantity_text"))
            for i in detail["ingredients"]
        ]

    lines = [f"*{title}* (#{ref})\n"]
    for name, qty_text in ingredient_rows:
        mark = "✓" if name in have_names else "✗"
        qty = f" ({qty_text})" if qty_text else ""
        lines.append(f"  {mark} {name}{qty}")
    if directions:
        lines.append(f"\n{directions}")
    return "\n".join(lines)


@router.message(_CHAN, _THR, Command("recipe"))
async def cmd_recipe(msg: Message):
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2 or _parse_ref(parts[1].strip()) is None:
        await msg.reply("Usage: /recipe <id> (get an id from /cook or /find; use a 'c' prefix for an ingested recipe)")
        return
    ref = parts[1].strip()
    text = await asyncio.get_event_loop().run_in_executor(None, _recipe_detail_text, ref)
    if text is None:
        await msg.reply(f"No recipe #{ref}.")
        return
    await msg.reply(text, parse_mode="Markdown")


def _matches_keyboard(matches: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=m["title"], callback_data=f"recipe:{m['id']}")]
        for m in matches
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Simple structured grammar tokens for /find and the free-text "find" intent's
# leftover text, e.g. "chicken active<30 type:dessert has:garlic,lemon".
_GRAMMAR_TOKEN_RE = re.compile(
    r"(active|total)<(\d+)|type:(\S+)|has:(\S+)", re.IGNORECASE
)


def _parse_find_grammar(text: str) -> tuple[str, dict]:
    """Strip structured tokens out of `text`, returning (remaining free-text,
    filters dict) for search_recipes()."""
    filters: dict = {}
    ingredients: set[str] = set()

    def _consume(m: re.Match) -> str:
        if m.group(1):  # active<30 / total<30
            minutes = int(m.group(2))
            key = "max_active_min" if m.group(1).lower() == "active" else "max_total_min"
            filters[key] = minutes
        elif m.group(3):  # type:dessert
            filters["category"] = m.group(3).lower()
        elif m.group(4):  # has:garlic,lemon
            for name in m.group(4).split(","):
                ingredients.add(canonicalize(name))
        return ""

    remaining = _GRAMMAR_TOKEN_RE.sub(_consume, text).strip()
    if ingredients:
        filters["ingredients"] = ingredients
    return remaining, filters


def _search_matches(
    query: str | None,
    filters: dict | None = None,
    limit: int = 8,
) -> list[dict]:
    """Merged corpus + custom-recipe search, used by /find and the "find" intent."""
    filters = filters or {}
    corpus = search_recipes(
        query=query or None,
        ingredients=filters.get("ingredients"),
        max_active_min=filters.get("max_active_min"),
        max_total_min=filters.get("max_total_min"),
        category=filters.get("category"),
        limit=limit,
        db_path=RECIPES_DB_PATH,
    )
    matches = [{"id": str(m["id"]), "title": m["title"]} for m in corpus]
    if query and len(matches) < limit:
        custom = custom_recipes_db.find_custom(query, limit=limit - len(matches), db_path=APP_DB_PATH)
        matches += [{"id": f"c{m['id']}", "title": m["title"]} for m in custom]
    return matches


async def _reply_find_results(msg: Message, query: str | None, filters: dict | None = None) -> None:
    matches = await asyncio.get_event_loop().run_in_executor(
        None, _search_matches, query, filters
    )
    label = query or "your search"
    if not matches:
        await msg.reply(f"No recipe found for \"{label}\". Try a different search.")
        return
    if len(matches) == 1:
        text = await asyncio.get_event_loop().run_in_executor(
            None, _recipe_detail_text, matches[0]["id"]
        )
        await msg.reply(text, parse_mode="Markdown")
        return
    await msg.reply(
        f"Found {len(matches)} recipes for \"{label}\" — pick one:",
        reply_markup=_matches_keyboard(matches),
    )


@router.message(_CHAN, _THR, Command("find"))
async def cmd_find(msg: Message):
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.reply(
            "Usage: /find <recipe name> [active<30] [total<45] [type:dessert] [has:garlic,lemon]"
        )
        return
    query, filters = _parse_find_grammar(parts[1].strip())
    await _reply_find_results(msg, query or None, filters)


@router.callback_query(F.message.chat.id == config.CHANNEL_ID, F.data.startswith("recipe:"))
async def on_recipe_picked(callback: CallbackQuery):
    ref = callback.data.split(":", 1)[1]
    text = await asyncio.get_event_loop().run_in_executor(None, _recipe_detail_text, ref)
    await callback.answer()
    if text is None:
        await callback.message.reply(f"No recipe #{ref}.")
        return
    await callback.message.reply(text, parse_mode="Markdown")


@router.message(_CHAN, _THR, Command("save"))
async def cmd_save(msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.reply("Usage: /save <id> [stars 1-5] (corpus recipes only, no 'c' prefix)")
        return
    recipe_id = int(parts[1])
    stars = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 5
    stars = max(1, min(5, stars))
    await asyncio.get_event_loop().run_in_executor(None, _rate_recipe, recipe_id, stars)
    await msg.reply(f"Saved #{recipe_id} at {stars}★.")


@router.message(_CHAN, _THR, Command("deleterecipe"))
async def cmd_delete_recipe(msg: Message):
    parts = (msg.text or "").split()
    parsed = _parse_ref(parts[1]) if len(parts) > 1 else None
    if parsed is None or parsed[0] != "custom":
        await msg.reply("Usage: /deleterecipe c<id> (only recipes you ingested can be deleted)")
        return
    _, recipe_id = parsed
    deleted = await asyncio.get_event_loop().run_in_executor(
        None, custom_recipes_db.delete_recipe, recipe_id, APP_DB_PATH
    )
    await msg.reply(f"Deleted c{recipe_id}." if deleted else f"No recipe c{recipe_id}.")


def _liked_recipe_ids() -> list[int]:
    conn = fridge_connect(APP_DB_PATH)
    try:
        rows = conn.execute("SELECT DISTINCT recipe_id FROM ratings").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _recommend_reply(top_n: int = 5) -> str:
    liked_ids = _liked_recipe_ids()
    if not liked_ids:
        return "Save a few recipes with /save <id> first, then I can recommend similar ones."
    liked_titles = [get_recipe(rid, db_path=RECIPES_DB_PATH) for rid in liked_ids[:3]]
    liked_titles = [d["title"] for d in liked_titles if d]

    results = recommend_similar(liked_ids, top_n=top_n)
    if not results:
        return "Couldn't find anything similar to your saved recipes yet."

    because = ", ".join(liked_titles) + ("…" if len(liked_ids) > 3 else "")
    lines = [f"🍽 *Because you liked {because}:*"]
    for r in results:
        lines.append(f"\n*#{r.recipe_id}* {r.title}  ({r.similarity:.0%} similar)")
    lines.append("\nTap /recipe <id> for the full recipe.")
    return "\n".join(lines)


@router.message(_CHAN, _THR, Command("recommend"))
async def cmd_recommend(msg: Message):
    reply = await asyncio.get_event_loop().run_in_executor(None, _recommend_reply)
    await msg.reply(reply, parse_mode="Markdown")


def _sub_reply(name: str) -> str:
    have = {
        i["canonical_ingredient"]
        for i in fridge_inventory.list_inventory(db_path=APP_DB_PATH)
    }
    subs = substitutions_db.lookup_or_ask(name, have_ingredients=have, db_path=APP_DB_PATH)
    if not subs:
        return f"No known substitute for {canonicalize(name)}."
    on_hand = subs & have
    lines = [f"Substitutes for *{canonicalize(name)}*:"]
    for s in sorted(subs):
        flag = " (you have this)" if s in on_hand else ""
        lines.append(f"  • {s}{flag}")
    return "\n".join(lines)


@router.message(_CHAN, _THR, Command("sub"))
async def cmd_sub(msg: Message):
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.reply("Usage: /sub <ingredient>, e.g. /sub buttermilk")
        return
    reply = await asyncio.get_event_loop().run_in_executor(None, _sub_reply, parts[1].strip())
    await msg.reply(reply, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Ingestion: pasted text ("ingest" intent, below) and photos/screenshots
# ---------------------------------------------------------------------------

def _save_ingested_recipe(extracted: dict, user_input: str | None) -> tuple[int, dict]:
    ingredients = []
    alternatives = {}
    for line in extracted.get("ingredients", []):
        canonical = canonicalize(line.get("canonical_guess") or line.get("raw_text", ""))
        if not canonical:
            continue
        ingredients.append({
            "canonical_name": canonical,
            "quantity_text": line.get("quantity_text"),
            "raw_text": line.get("raw_text"),  # kept for etl/promote_custom_recipes.py's re-parse
        })
        alts = [canonicalize(a) for a in line.get("alternatives") or [] if a]
        if alts:
            alternatives[canonical] = alts
            for alt in alts:
                substitutions_db.add_substitution(canonical, alt, source="ingested", db_path=APP_DB_PATH)

    title = extracted.get("title") or "Untitled recipe"
    recipe_id = custom_recipes_db.save_recipe(
        title, ingredients, directions=extracted.get("directions"),
        alternatives=alternatives, user_input=user_input, db_path=APP_DB_PATH,
    )
    return recipe_id, {"title": title, "ingredients": ingredients, "alternatives": alternatives}


def _ingest_summary(recipe_id: int, saved: dict) -> str:
    lines = [f"📖 Saved *{saved['title']}* as #c{recipe_id}"]
    for ing in saved["ingredients"]:
        alt = saved["alternatives"].get(ing["canonical_name"])
        alt_str = f"  (or {', '.join(alt)})" if alt else ""
        qty = f"{ing['quantity_text']} " if ing["quantity_text"] else ""
        lines.append(f"  • {qty}{ing['canonical_name']}{alt_str}")
    lines.append(f"\nLooks wrong? /deleterecipe c{recipe_id} and try again.")
    return "\n".join(lines)


@router.message(_CHAN, _THR, F.photo)
async def handle_recipe_photo(msg: Message):
    try:
        photo = msg.photo[-1]
        file = await msg.bot.get_file(photo.file_id)
        bio = await msg.bot.download_file(file.file_path)
        extracted = await extract_from_image(bio.read())
    except ValueError:
        await msg.reply("Couldn't find a recipe in that image.")
        return
    except Exception:
        log.exception("Recipe image ingestion failed")
        await msg.reply("Couldn't read that image.")
        return

    recipe_id, saved = await asyncio.get_event_loop().run_in_executor(
        None, _save_ingested_recipe, extracted, "[photo]"
    )
    await msg.reply(_ingest_summary(recipe_id, saved), parse_mode="Markdown")


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
    filters = data.get("filters") or {}
    if filters.get("ingredients"):
        filters["ingredients"] = {canonicalize(n) for n in filters["ingredients"]}

    if intent == "show":
        await msg.reply(_fridge_text(), parse_mode="Markdown")
        return

    if intent == "cook":
        reply = await asyncio.get_event_loop().run_in_executor(None, _cook_reply)
        await msg.reply(reply, parse_mode="Markdown")
        return

    if intent == "find":
        dish = items[0]["name"] if items else None
        await _reply_find_results(msg, dish, filters)
        return

    if intent == "substitute":
        name = items[0]["name"] if items else msg.text
        reply = await asyncio.get_event_loop().run_in_executor(None, _sub_reply, name)
        await msg.reply(reply, parse_mode="Markdown")
        return

    if intent == "ingest":
        try:
            extracted = await extract_from_text(msg.text)
        except ValueError:
            await msg.reply("That didn't look like a recipe I could parse.")
            return
        except Exception:
            log.exception("Recipe text ingestion failed")
            await msg.reply("Couldn't parse that recipe.")
            return
        recipe_id, saved = await asyncio.get_event_loop().run_in_executor(
            None, _save_ingested_recipe, extracted, msg.text
        )
        await msg.reply(_ingest_summary(recipe_id, saved), parse_mode="Markdown")
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
        "Tell me what you bought, ate, or removed, ask \"what can I make?\", or paste "
        "a recipe to save it. Use /fridge to see inventory, /cook for suggestions, "
        "/help for the full command list."
    )
