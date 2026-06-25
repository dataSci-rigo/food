"""
GI lookup cascade:
  1. gi_cache  (SQLite, instant)
  2. Open Food Facts  (if barcode given)
  3. gi_db.json  (bundled ~200-food table, fuzzy match)
  4. Macro-ratio estimate  (sugar/fiber + carbs/fiber, if nutrition data provided)
  5. Claude estimate  (haiku, last resort)
"""

from __future__ import annotations

import json
import os
import re
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_GI_DB_PATH = os.path.join(_HERE, "gi_db.json")

_gi_db: dict[str, float] | None = None


def _load_gi_db() -> dict[str, float]:
    global _gi_db
    if _gi_db is None:
        with open(_GI_DB_PATH) as f:
            _gi_db = json.load(f)
    return _gi_db


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def _bundled_lookup(food_name: str) -> float | None:
    db = _load_gi_db()
    key = _normalize(food_name)
    if key in db:
        return db[key]
    # Partial / substring match — longest matching key wins
    best_key = None
    best_len = 0
    for k in db:
        if k in key or key in k:
            if len(k) > best_len:
                best_key = k
                best_len = len(k)
    return db[best_key] if best_key else None


def _off_gi(barcode: str) -> float | None:
    try:
        url = f"https://world.openfoodfacts.org/api/v2/product/{barcode}.json"
        r = requests.get(url, timeout=8,
                         headers={"User-Agent": "food-bot/0.1"})
        r.raise_for_status()
        data = r.json()
        if data.get("status") != 1:
            return None
        n = data["product"].get("nutriments", {})
        gi = n.get("glycemic-index_100g") or n.get("glycemic_index_100g")
        return float(gi) if gi is not None else None
    except Exception:
        return None


def _macro_ratio_estimate(
    carbs_g: float | None,
    sugar_g: float | None,
    fiber_g: float | None,
) -> float | None:
    """
    Estimate GI from sugar-to-fiber and carbs-to-fiber ratios.
    Fiber slows carb absorption → higher ratio = higher GI.
    Returns None if carbs data is unavailable.
    """
    if not carbs_g or carbs_g < 1:
        return None

    # Floor fiber at 0.5g so zero-fiber foods get high (not infinite) ratios
    fiber = max(fiber_g or 0, 0.5)
    sugar = sugar_g or 0

    carbs_fiber = min(carbs_g / fiber, 40)   # cap: no-fiber refined foods max out here
    sugar_fiber = min(sugar / fiber, 25)      # cap: pure-sugar foods max out here

    gi = 30 + carbs_fiber * 1.4 + sugar_fiber * 1.1
    return round(min(max(gi, 20), 95))


def _claude_estimate(food_name: str) -> float | None:
    try:
        import anthropic
        import config
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system="Return only a single integer (0-100) for the glycemic index. No explanation.",
            messages=[{"role": "user", "content": f"Glycemic index of: {food_name}"}],
        )
        text = msg.content[0].text.strip()
        m = re.search(r"\d+", text)
        return float(m.group()) if m else None
    except Exception:
        return None


def lookup_gi(
    food_name: str,
    barcode: str | None = None,
    carbs_g: float | None = None,
    sugar_g: float | None = None,
    fiber_g: float | None = None,
) -> tuple[float | None, str]:
    """
    Returns (gi_value, source) where source is one of:
    "cache" | "off" | "bundled" | "macro_ratio" | "claude" | "unknown"
    Pass carbs_g/sugar_g/fiber_g to enable the macro-ratio fallback before Claude.
    """
    import db
    key = barcode if barcode else _normalize(food_name)

    # 1. cache
    cached = db.gi_get(key)
    if cached:
        return cached[0], "cache"

    gi: float | None = None
    source = "unknown"

    # 2. Open Food Facts (barcode only)
    if barcode:
        gi = _off_gi(barcode)
        if gi is not None:
            source = "off"

    # 3. bundled table
    if gi is None:
        gi = _bundled_lookup(food_name)
        if gi is not None:
            source = "bundled"

    # 4. macro-ratio estimate (sugar/fiber + carbs/fiber), no API call needed
    if gi is None:
        gi = _macro_ratio_estimate(carbs_g, sugar_g, fiber_g)
        if gi is not None:
            source = "macro_ratio"

    # 5. Claude estimate (last resort)
    if gi is None:
        gi = _claude_estimate(food_name)
        if gi is not None:
            source = "claude"

    if gi is not None:
        db.gi_set(key, gi, source)

    return gi, source
