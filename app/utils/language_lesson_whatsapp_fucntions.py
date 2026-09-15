import httpx
import logging

from fastapi import HTTPException
from app.settings.config import settings
from app.services.openai_client import client
from app.utils.chat_memory import get_conversation_history, trim_history, save_message

logger = logging.getLogger(__name__)

from app.utils.language_lesson_utils import format_lesson_card



def _get_words() -> list:
    """Fetch language lesson words from the cache (AI Material sheet)."""
    try:
        from app.services.menu_services import get_language_lesson_words
        return get_language_lesson_words()
    except Exception as e:
        logger.warning(f"Could not load language lesson words from cache: {e}")
        return []


def _format_lesson_card(word_data: dict, index: int) -> str:
    """WhatsApp-specific formatting wrap (plaintext)."""
    return format_lesson_card(word_data, index, platform="whatsapp")



async def _send_lesson_with_buttons(sender_id: str, body_text: str) -> None:
    """Send an interactive button message with ✅ Next Word / 🏁 Stop for lesson cycling."""
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "language_yes", "title": "✅ Next Word"}},
                    {"type": "reply", "reply": {"id": "language_no",  "title": "🏁 Stop"}},
                ]
            },
        },
    }
    async with httpx.AsyncClient() as http:
        resp = await http.post(settings.whatsapp_api_url, json=payload, headers=headers)
        resp.raise_for_status()


async def language_starting_message(sender_id: str) -> None:
    """Send the first lesson card (word index 0) with an engaging intro message."""
    words = _get_words()
    if not words:
        from app.utils.whatsapp_func import send_whatsapp_message
        await send_whatsapp_message(
            sender_id,
            "Sorry, the language lesson content is being updated. Please try again shortly.",
        )
        return

    card = _format_lesson_card(words[0], 0)
    intro = (
        "Hi! Ready for your first language lesson of the day? 🎉\n"
        "Or feel free to ask us about any word or phrase you're curious about – "
        "We're happy to help you with that too! 😊\n\n"
        "Today's first word is:\n\n"
        + card
        + "\n\nReady to learn the next one?"
    )
    await _send_lesson_with_buttons(sender_id, intro)


async def language_yes_message(sender_id: str, word_index: int = 1) -> None:
    """Send the lesson card at the given word_index (wraps around at end)."""
    words = _get_words()
    if not words:
        from app.utils.whatsapp_func import send_whatsapp_message
        await send_whatsapp_message(sender_id, "Sorry, lesson content unavailable. Try again shortly.")
        return

    idx = word_index % len(words)

    # Completed a full cycle
    if word_index > 0 and idx == 0:
        from app.utils.whatsapp_func import send_whatsapp_message
        await send_whatsapp_message(
            sender_id,
            "🎊 *Congratulations!* You've completed all the words for today!\n\n"
            "You're amazing — the Balinese people will be so impressed! 🙏🌴\n\n"
            "Starting from the beginning for more practice..."
        )

    card = _format_lesson_card(words[idx], idx)

    if idx == 0:
        prefix = "Let's start again from the top! 🔄\n\n"
    elif idx == len(words) - 1:
        prefix = "🏆 Last word of the lesson! You've almost made it!\n\n"
    else:
        prefix = "Here's your next word:\n\n"

    body = prefix + card + "\n\nReady for another one?"
    await _send_lesson_with_buttons(sender_id, body)


async def language_no_message(sender_id: str) -> None:
    """Send the 'What next?' options after user taps Stop."""
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": (
                    "Great effort today! 🌟\n\n"
                    "You can ask me any Balinese or Indonesian word or phrase anytime — "
                    "just type it and I'll translate it for you! 💬\n\n"
                    "What would you like to do next?"
                )
            },
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "language_phrase", "title": "💬 Ask a Phrase"}},
                    {"type": "reply", "reply": {"id": "back_to_menu",    "title": "🔙 Back to Menu"}},
                ]
            },
        },
    }
    async with httpx.AsyncClient() as http:
        resp = await http.post(settings.whatsapp_api_url, json=payload, headers=headers)
        resp.raise_for_status()


async def language_lesson_response(query: str, user_id: str) -> str:
    """
    Freestyle mode: try sheet lookup first, then AI fallback.
    Searches for matching words in the lesson sheet before hitting OpenAI.
    """
    words = _get_words()

    # Sheet lookup: check if any word (English, Indonesian, or Balinese) matches query
    if words:
        query_lower = query.lower()
        for i, word_data in enumerate(words):
            for col in ("English", "Indonesian", "Balinese"):
                val = str(word_data.get(col, "")).lower().strip()
                if val and val not in ("nan", "") and val in query_lower:
                    card = _format_lesson_card(word_data, i)
                    return (
                        "Found it in today's lesson! 🎓\n\n"
                        + card
                        + "\n\nFeel free to ask about any other word or phrase! 😊"
                    )

    # AI fallback — warm, class-style tutor
    chat_history = get_conversation_history(user_id)
    conversation = trim_history(chat_history + [{"role": "user", "content": query}])

    prompt = (
        "You are Kadek, a warm, enthusiastic Balinese and Indonesian language tutor for tourists visiting Bali.\n"
        "Your style: encouraging, friendly, culturally rich — like a fun classroom session.\n"
        "For each phrase or question:\n"
        "- Give the English meaning first.\n"
        "- Then show the Indonesian version with phonetic pronunciation.\n"
        "- Then show the Balinese version with phonetic pronunciation (if different).\n"
        "- Add a short tip on WHEN or HOW to use it (cultural context).\n"
        "- End with a warm, motivating line (e.g. 'The Balinese people will love hearing you say this!').\n"
        "Format for WhatsApp — plain text only, NO markdown symbols (*, #, -, etc).\n"
        "Be concise but complete. Max 5-6 lines."
    )

    try:
        completion = await client.chat.completions.create(
            model=settings.OPENAI_MODEL_NAME,
            messages=[
                {"role": "system", "content": prompt},
                *[{"role": m["role"], "content": m["content"]} for m in conversation[-6:]],
                {"role": "user", "content": query},
            ],
            max_tokens=400,
            temperature=0.5,
        )
        response = completion.choices[0].message.content
        save_message(user_id, "user", query)
        save_message(user_id, "assistant", response)
        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating response: {e}")
