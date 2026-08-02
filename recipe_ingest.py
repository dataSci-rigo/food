"""
Extract a recipe (title, ingredients incl. substitution alternatives,
directions) from a photo/screenshot or pasted text, using Claude -- same
shape as ocr.py's nutrition-label reader (strict JSON-extraction prompt,
markdown-fence stripping, raises ValueError on a miss).

Deliberately does NOT invoke the local ingredient-parser-nlp model: Claude
supplies its own best-guess canonical ingredient name per line, which then
only needs the cheap part of core.normalize (canonicalize(): descriptor
stripping + inflect + alias lookup, no model load) to line up with the rest
of the vocabulary. Keeps the e2-micro VM out of the heavy-parsing business;
the trained parser only runs later, in etl/promote_custom_recipes.py on the
laptop.
"""

from __future__ import annotations

import base64
import json
import re

import anthropic
import config

_SYSTEM = """\
You extract recipes from text or images. Return ONLY valid JSON:
{
  "title": "Recipe Name",
  "ingredients": [
    {"raw_text": "1 cup butter, or margarine", "quantity_text": "1 cup",
     "canonical_guess": "butter", "alternatives": ["margarine"]}
  ],
  "directions": "Step-by-step instructions as one text block."
}
- raw_text: the ingredient line as written/read, verbatim.
- quantity_text: just the amount+unit portion, e.g. "1 cup", "2 tbsp", null if none.
- canonical_guess: your best simple/generic name for the ingredient -- lowercase,
  singular, with descriptors like "fresh"/"chopped"/"large" removed, e.g.
  "2 cups all-purpose flour, sifted" -> "all purpose flour".
- alternatives: any "or X" / "you can substitute Y" alternatives mentioned on that
  same line, as canonical-style names (same rules as canonical_guess). Empty list
  if the line names only one ingredient.
- directions: cooking instructions as one text block. Empty string if genuinely
  not present in the input.
If the input does not contain a recipe at all, return: {"error": "no recipe found"}
No prose, no markdown -- raw JSON only."""


def _parse_response(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    data = json.loads(raw)
    if data.get("error"):
        raise ValueError("No recipe found in input")
    return data


async def extract_from_image(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    },
                    {"type": "text", "text": "Extract the recipe from this image."},
                ],
            }
        ],
    )
    return _parse_response(msg.content[0].text)


async def extract_from_text(text: str) -> dict:
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    return _parse_response(msg.content[0].text)
