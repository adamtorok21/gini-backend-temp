import logging
from app.services.menu_services import get_language_lesson_words

logger = logging.getLogger(__name__)

# Encouragement phrases shown after each word card
ENCOURAGEMENTS = [
    "You're doing great! Keep it up! 🌟",
    "Wonderful! Every word brings you closer to the locals! 🤝",
    "Excellent! The Balinese people will love hearing you try! 🙏",
    "Amazing! You're becoming a language star! ⭐",
    "Fantastic! Learning a new language opens new doors! 🚪",
    "Brilliant! Soon you'll be chatting like a local! 🌴",
    "Superb! The more you learn, the richer your experience! 🌺",
]

# Fun facts to spark curiosity
FUN_FACTS = [
    "💡 *Fun fact:* Balinese and Indonesian are different languages — Balinese is only spoken in Bali!",
    "💡 *Did you know?* Saying even one word in the local language makes Balinese people smile warmly.",
    "💡 *Tip:* Balinese people LOVE when tourists try to speak their language — don't be shy!",
    "💡 *Cultural note:* Bali has its own script, alphabet, and calendar — truly unique!",
    "💡 *Tip:* The Balinese greeting 'Om Swastiastu' shows deep respect and is always appreciated.",
]

def format_lesson_card(word_data: dict, index: int, platform: str = "web") -> str:
    """Format a language lesson card with Markdown for Web or Plaintext for WhatsApp."""
    english  = str(word_data.get("English", "")).strip()
    indonesian = str(word_data.get("Indonesian", "")).strip()
    id_pron  = str(word_data.get("Indonesian Pronunciation", "")).strip()
    balinese = str(word_data.get("Balinese", "")).strip()
    bal_pron = str(word_data.get("Balinese Pronunciation", "")).strip()
    context  = str(word_data.get("Cultural Context", "")).strip()

    is_web = platform == "web"
    bold = "**" if is_web else "*"
    ital = "" if is_web else "_" # Web markdown is better without underscores for pronunciation blocks

    lines = []

    # English meaning header (no word count shown)
    lines.append(f"✨ {bold}{english}{bold}")
    lines.append("")

    # Indonesian
    if indonesian:
        lines.append(f"🇮🇩 {bold}Indonesian:{bold} {indonesian}")
        if id_pron:
            lines.append(f"   🔊 Say it: {ital}{id_pron}{ital}")

    # Balinese
    if balinese and balinese.lower() not in ("nan", ""):
        lines.append(f"🌺 {bold}Balinese:{bold} {balinese}")
        if bal_pron and bal_pron.lower() not in ("nan", ""):
            lines.append(f"   🔊 Say it: {ital}{bal_pron}{ital}")

    # Example / cultural context
    if context and context.lower() not in ("nan", ""):
        lines.append("")
        lines.append(f"📖 {bold}Example:{bold} {context}")

    # Random encouragement
    lines.append("")
    lines.append(ENCOURAGEMENTS[index % len(ENCOURAGEMENTS)])

    # Periodic fun fact
    if index % 3 == 2:
        lines.append("")
        fact = FUN_FACTS[(index // 3) % len(FUN_FACTS)]
        if is_web:
             fact = fact.replace("*", "**") # Bold conversion
        lines.append(fact)

    return "\n".join(lines)

def get_web_lesson_payload(index: int = 0) -> dict:
    """Returns the full Web card response including content and navigation buttons."""
    words = get_language_lesson_words()
    if not words:
        return {
            "response": "Sorry, our language lesson dictionary is currently being updated. Please check back in a moment! 😊",
            "nav_options": None
        }

    total = len(words)
    idx = index % total

    card_text = format_lesson_card(words[idx], idx, platform="web")

    # Handle first word / wrap around
    if idx == 0 and index == 0:
        header = (
            "Hi! Ready for your first language lesson of the day? 🎉\n"
            "Or feel free to ask us about any word or phrase you're curious about – "
            "We're happy to help you with that too! 😊\n\n"
            "Today's first word is:\n\n"
        )
    elif idx == 0:
        header = "🎊 **Congratulations!** You've completed all the words for today!\n\nStarting from the beginning for more practice... 🔄\n\n"
    elif idx == total - 1:
        header = "🏆 Last word of the lesson! You've almost made it!\n\n"
    else:
        header = "Here's your next word:\n\n"

    full_text = header + card_text + "\n\nReady for another one?"
    
    # Buttons for the Web Chat
    next_index = idx + 1
    nav_options = {
        "title": "",
        "options": [
            {"label": "✅ Next Word", "value": f"LANGUAGE_NEXT_{next_index}"},
            {"label": "🚩 Stop", "value": "LANGUAGE_STOP"}
        ]
    }
    
    return {
        "response": full_text,
        "nav_options": nav_options
    }
