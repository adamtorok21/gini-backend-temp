"""
Sheet-driven guest-facing text translation — WCR-I18N-DYNAMIC-01.

Web-only, purely additive. Google Sheets (Menu Design, Menu Structure,
Services Designs, Prices Set/Services Overview, D&P) are always authored in
English. The website's EN/ID language toggle (LanguageContext) already
translates every static UI string via translations.json, but text pulled
live from these sheets was never translated — this module closes that gap
for the specific web routes that opt in via an explicit `language` param.

Never touches WhatsApp code paths: WhatsApp callers never pass `language`,
so every WhatsApp-facing function (get_categories_only, get_category_sections,
get_service_items_for_whatsapp, and the D&P Meta Flow handlers) is completely
unaffected by this module's existence.

Safety rule (do not violate): callers must only ever apply translate_fields()
to *_display copies of a field, never to the original field used as a lookup
key for subsequent navigation, price matching, or booking. See CLAUDE.md
"Dynamic Sheet-Text Translation" invariants.
"""

import asyncio
import hashlib
import logging
from typing import Optional, Union

from app.db.session import translation_cache_collection
from app.services.openai_client import client

logger = logging.getLogger(__name__)

# Only Indonesian is supported today — the site's only non-English UI language.
_SUPPORTED_TARGETS = {"ID"}

_SYSTEM_PROMPT = (
    "Translate the given text from English to Indonesian for a Bali tourism/"
    "concierge app. Return ONLY the translated text — no quotes, no "
    "explanation, no extra content. Preserve tone and keep it short, matching "
    "the style of catalog/menu copy."
)


def _cache_key(text: str, target_lang: str) -> str:
    return hashlib.sha256(f"{target_lang}:{text}".encode("utf-8")).hexdigest()


async def translate_text(text: str, target_lang: str) -> str:
    """Translate a single string, backed by a MongoDB cache.

    Non-fatal: any failure (cache lookup, OpenAI call, cache write) returns
    the original English text so a translation hiccup never blocks a guest
    from seeing content.
    """
    if not text or not isinstance(text, str) or not text.strip():
        return text
    if target_lang not in _SUPPORTED_TARGETS:
        return text

    key = _cache_key(text, target_lang)

    try:
        cached = await translation_cache_collection.find_one({"_id": key})
        if cached and cached.get("translated_text"):
            return cached["translated_text"]
    except Exception as e:
        logger.warning(f"[translation] cache lookup failed for '{text[:40]}...': {e}")

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
            max_tokens=500,
        )
        translated = (response.choices[0].message.content or "").strip()
        if not translated:
            return text
    except Exception as e:
        logger.warning(f"[translation] OpenAI translate failed for '{text[:40]}...': {e}")
        return text

    try:
        await translation_cache_collection.update_one(
            {"_id": key},
            {
                "$set": {
                    "source_text": text,
                    "target_lang": target_lang,
                    "translated_text": translated,
                }
            },
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"[translation] cache write failed for '{text[:40]}...': {e}")

    return translated


async def translate_fields(
    items: Union[dict, list, None],
    fields: list,
    language: str,
    suffix: str = "_display",
) -> Union[dict, list, None]:
    """Add a `<field><suffix>` translated copy for each of `fields` on every
    dict in `items`, when language == "ID". No-op (returns items unchanged)
    for any other language, including the default "EN".

    The original fields are never modified — callers that use a field as a
    lookup key (e.g. re-fetching subcategories, matching a service item
    against Prices Set, or building a booking payload) must keep reading the
    original field; only the new `<field>_display` key is meant for
    rendering.

    `items` may be a single dict, a list of dicts, or None — always returned
    in the same shape it was given.
    """
    if items is None or language not in _SUPPORTED_TARGETS:
        return items

    single = isinstance(items, dict)
    rows = [items] if single else list(items)

    async def _translate_row(row: dict) -> dict:
        new_row = dict(row)
        for field in fields:
            val = row.get(field)
            if isinstance(val, str) and val.strip():
                new_row[f"{field}{suffix}"] = await translate_text(val, language)
        return new_row

    translated_rows = await asyncio.gather(*[_translate_row(r) for r in rows])
    return translated_rows[0] if single else list(translated_rows)
