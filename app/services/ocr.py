"""Vision API roster line extraction."""

import base64

import httpx

from app.config import get_settings

_ROSTER_PROMPT = (
    "Верни только строки с людьми с фотографии списка, по одной строке на человека, "
    "без нумерации и без пояснений."
)


async def extract_roster_lines(image_bytes: bytes, mime: str) -> list[str]:
    settings = get_settings()
    if not settings.VISION_API_KEY or not settings.VISION_API_URL:
        raise RuntimeError("ocr_not_configured")

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
