"""Roster line extraction from a photo.

Uses a vision API when VISION_API_KEY and VISION_API_URL are set.
Otherwise reads the image locally with Tesseract (rus+eng).
"""

import asyncio
import base64
import io
import re

import httpx

from app.config import get_settings

_ROSTER_PROMPT = (
    "Верни только строки с людьми с фотографии списка, по одной строке на человека, "
    "без нумерации и без пояснений."
)


_HYPHEN_CHARS = str.maketrans({"—": "-", "–": "-", "−": "-", "‐": "-"})


def clean_local_ocr_line(line: str) -> str:
    """Keep letters, spaces, and a hyphen between letters (double surnames)."""
    line = line.translate(_HYPHEN_CHARS)
    chars: list[str] = []
    for char in line:
        if char.isalpha() or char == "-" or char.isspace():
            chars.append(char)
        else:
            chars.append(" ")
    line = re.sub(r"\s+", " ", "".join(chars)).strip()
    line = re.sub(r"(?<!\w)-+|-+(?!\w)", "", line)
    return re.sub(r"\s+", " ", line).strip()


def _tesseract_lines(image_bytes: bytes) -> list[str]:
    import pytesseract
    from PIL import Image

    image = Image.open(io.BytesIO(image_bytes))
    text = pytesseract.image_to_string(image, lang="rus+eng")
    lines: list[str] = []
    for raw in text.splitlines():
        cleaned = clean_local_ocr_line(raw)
        if cleaned:
            lines.append(cleaned)
    return lines


async def extract_roster_lines(image_bytes: bytes, mime: str) -> list[str]:
    settings = get_settings()
    if settings.VISION_API_KEY and settings.VISION_API_URL:
        return await _vision_lines(image_bytes, mime, settings)
    return await asyncio.to_thread(_tesseract_lines, image_bytes)


async def _vision_lines(image_bytes: bytes, mime: str, settings) -> list[str]:
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    payload = {
        "model": settings.VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": _ROSTER_PROMPT},
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {settings.VISION_API_KEY}"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            settings.VISION_API_URL,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        body = response.json()

    content = body["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("unexpected_vision_response")
    return [line.strip() for line in content.splitlines() if line.strip()]
