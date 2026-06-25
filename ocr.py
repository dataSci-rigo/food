"""
Read a nutrition facts label from an image using Claude vision.
Returns a dict with parsed nutrient values, or raises ValueError if no label found.
"""

from __future__ import annotations

import base64
import json
import re

import anthropic
import config

_SYSTEM = """\
You are a nutrition label OCR assistant. The user sends an image.
If the image contains a Nutrition Facts label, extract the values and return ONLY valid JSON with these keys:
  food_name, serving_g, calories, sat_fat_g, sodium_mg, carbs_g, sugar_g, glycemic_index
Use null for any field not shown on the label. serving_g should be the serving size in grams.
If the image does NOT contain a nutrition label, return: {"error": "no label"}
No prose, no markdown — raw JSON only."""


async def read_label(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_KEY)
    b64 = base64.standard_b64encode(image_bytes).decode()
    msg = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    },
                    {"type": "text", "text": "Read the nutrition label in this image."},
                ],
            }
        ],
    )
    raw = msg.content[0].text.strip()
    # Strip markdown fences if model adds them
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)
    data = json.loads(raw)
    if data.get("error"):
        raise ValueError("No nutrition label detected in image")
    return data
