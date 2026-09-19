"""
Telegram reference-finder bot for photographers.

Flow:
  photo and/or text -> Gemini vision -> structured search queries
  -> Pexels search (real images, inline album) + Pinterest search links (buttons)

Install:  pip install "aiogram>=3.13" httpx google-genai
Env vars: TELEGRAM_TOKEN, GEMINI_API_KEY, PEXELS_API_KEY
Optional: GEMINI_MODEL (default: gemini-3.6-flash)
Run:      python bot.py
"""
import asyncio
import io
import json
import logging
import os
import re
from urllib.parse import quote_plus

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from google import genai
from google.genai import types, errors as genai_errors

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("refbot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

bot = Bot(TELEGRAM_TOKEN)
dp = Dispatcher()
gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

SYSTEM_PROMPT = """You help photographers find reference images for photoshoots.
Given a photo and/or a text description (any language), analyse the visual
qualities that matter for a shoot: subject, pose, composition, framing, lighting,
color palette, location/setting, mood, styling.

Return ONLY a JSON object, no markdown fences:
{
  "summary": "one or two sentences describing the look, in the user's language",
  "queries": ["3 to 5 short English search queries, 2-5 words each, varied
               (e.g. one about pose, one about lighting, one about location/mood)"]
}"""

async def analyze(text: str | None, image: bytes | None) -> dict:
    parts = []
    if image:
        # Telegram re-encodes photos as JPEG
        parts.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))
    parts.append(text or "Find references similar to this photo.")
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        max_output_tokens=4000,
    )

    delays = [2, 5, 10]
    for attempt in range(len(delays) + 1):
        try:
            resp = await gemini.aio.models.generate_content(
                model=MODEL, contents=parts, config=config
            )
            break
        except genai_errors.ServerError:  # 5xx, e.g. 503 "high demand"
            if attempt == len(delays):
                raise
            log.warning("Gemini overloaded, retrying in %ss", delays[attempt])
            await asyncio.sleep(delays[attempt])

    match = re.search(r"\{.*\}", resp.text, re.DOTALL)
    data = json.loads(match.group(0))
    data["queries"] = [q.strip() for q in data.get("queries", []) if q.strip()][:5]
    return data

async def pexels_search(client: httpx.AsyncClient, query: str, n: int = 4) -> list[dict]:
    try:
        r = await client.get(
            "https://api.pexels.com/v1/search",
            params={"query": query, "per_page": n, "orientation": "portrait"},
            headers={"Authorization": PEXELS_API_KEY},
        )
        r.raise_for_status()
        return r.json().get("photos", [])
    except Exception:
        log.exception("Pexels search failed for %r", query)
        return []


def pinterest_url(query: str) -> str:
    return f"https://www.pinterest.com/search/pins/?q={quote_plus(query)}"


async def respond(m: Message, text: str | None, image: bytes | None = None) -> None:
    status = await m.answer("🔎 Analysing your reference…")
    try:
        info = await analyze(text, image)
    except Exception:
        log.exception("Analysis failed")
        await status.edit_text(
            "This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later."
        )
        return

    queries = info["queries"]
    async with httpx.AsyncClient(timeout=15) as client:
        results = await asyncio.gather(*(pexels_search(client, q) for q in queries))

    seen, photos = set(), []
    for batch in results:
        for p in batch:
            if p["id"] not in seen:
                seen.add(p["id"])
                photos.append(p)
    photos = photos[:8]

    await status.delete()

    if photos:
        album = [
            InputMediaPhoto(
                media=p["src"]["large"],
                caption=f"📷 {p['photographer']} · {p['url']}",
            )
            for p in photos
        ]
        try:
            await m.answer_media_group(album)
        except Exception:
            log.exception("Album send failed")

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"📌 {q}", url=pinterest_url(q))] for q in queries
        ]
    )
    await m.answer(
        f"{info.get('summary', '')}\n\nOpen these searches on Pinterest:",
        reply_markup=keyboard,
    )


@dp.message(CommandStart())
async def start(m: Message) -> None:
    await m.answer(
        "Hi! Send me a photo or describe the shot you have in mind "
        "(you can also add a caption to a photo), and I'll find reference "
        "images and Pinterest searches for it."
    )


@dp.message(F.photo)
async def on_photo(m: Message) -> None:
    buf = io.BytesIO()
    await bot.download(m.photo[-1], destination=buf)
    await respond(m, m.caption, buf.getvalue())


@dp.message(F.text)
async def on_text(m: Message) -> None:
    await respond(m, m.text)


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
