#!/usr/bin/env python3
"""
Smoke tests — real API calls, no mocking.
Run from the food/ directory:  python tests/smoke_apis.py

Each check prints PASS / FAIL and a short reason.
Exit code: 0 if all pass, 1 if any fail.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
results: list[tuple[str, bool, str]] = []


def check(label: str):
    def decorator(fn):
        try:
            fn()
            results.append((label, True, ""))
            print(f"  {PASS}  {label}")
        except Exception as e:
            results.append((label, False, str(e)))
            print(f"  {FAIL}  {label}")
            print(f"        {e}")
        return fn
    return decorator


print("\n=== Food Bot Smoke Tests ===\n")

# ------------------------------------------------------------------
# 1. DB init
# ------------------------------------------------------------------
@check("DB initializes without error")
def _():
    import config, db
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        import monkeypatch_helper  # not real — just patch inline
    except ImportError:
        pass
    original = config.DB_PATH
    config.DB_PATH = path
    try:
        db.init_db()
    finally:
        config.DB_PATH = original
        os.unlink(path)


# ------------------------------------------------------------------
# 2. USDA — plain name
# ------------------------------------------------------------------
@check("USDA: lookup 'banana' returns calories")
def _():
    from nutrition import usda_search
    result = usda_search("banana")
    assert result is not None, "got None"
    assert result.calories and result.calories > 0, f"bad calories: {result.calories}"


# ------------------------------------------------------------------
# 3. USDA — freeform fallback (even when Ninjas key is set)
# ------------------------------------------------------------------
@check("USDA: lookup '2 eggs' returns result")
def _():
    from nutrition import usda_search
    result = usda_search("2 eggs")
    assert result is not None, "got None"


# ------------------------------------------------------------------
# 4. USDA fallback when API Ninjas key absent
# ------------------------------------------------------------------
@check("lookup() freeform falls back to USDA when API_NINJAS_API_KEY='testfail'")
def _():
    import nutrition
    original_key = nutrition.API_NINJAS_API_KEY
    nutrition.API_NINJAS_API_KEY = ""   # simulate missing key
    try:
        result = nutrition.lookup("2 eggs and toast")
        assert result is not None, "got None"
        assert result.source == "usda", f"expected usda, got {result.source}"
    finally:
        nutrition.API_NINJAS_API_KEY = original_key


# ------------------------------------------------------------------
# 5. Open Food Facts — barcode
# ------------------------------------------------------------------
@check("Open Food Facts: barcode 3017620422003 (Nutella) returns a result")
def _():
    from nutrition import openfoodfacts_by_barcode
    result = openfoodfacts_by_barcode("3017620422003")
    assert result is not None, "product not found"
    assert result.calories and result.calories > 0, f"bad calories: {result.calories}"


# ------------------------------------------------------------------
# 6. API Ninjas (only if key is present)
# ------------------------------------------------------------------
@check("API Ninjas / lookup() freeform returns non-None calories (key set)")
def _():
    import nutrition
    if not nutrition.API_NINJAS_API_KEY:
        print("       (no API_NINJAS_API_KEY — skipping)", end="")
        return
    # Use lookup() not apininjas_nutrition() — we want to test the fallback path too.
    # Free-tier keys return calories=None; lookup() should fall back to USDA in that case.
    result = nutrition.lookup("chicken breast", kind="freeform")
    assert result is not None, "lookup() returned None"
    assert result.calories is not None, (
        f"calories is None (API Ninjas free tier?); source={result.source}; "
        "check that USDA fallback is working"
    )
    assert result.calories > 0, f"calories={result.calories}"


# ------------------------------------------------------------------
# 7. Claude API reachable
# ------------------------------------------------------------------
@check("Anthropic API: minimal message create succeeds")
def _():
    import anthropic, config
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        messages=[{"role": "user", "content": "reply with the word ok"}],
    )
    assert resp.content, "empty response"


# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
print()
passed = sum(1 for _, ok, _ in results if ok)
total  = len(results)
print(f"Results: {passed}/{total} passed")
if passed < total:
    print("\nFailed checks:")
    for label, ok, reason in results:
        if not ok:
            print(f"  • {label}: {reason}")
    sys.exit(1)
else:
    print("All smoke tests passed ✓")
