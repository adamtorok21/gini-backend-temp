import json
import logging
import re
import pandas as pd
from typing import Dict, Any, List, Optional
from fastapi import HTTPException
from app.settings.config import settings
from app.services.menu_services import cache
from app.services.openai_client import client
from app.utils.chat_memory import get_conversation_history, trim_history, save_message
from app.services.pinconeservice import get_index
from app.utils.navigation_rules import rules
from app.services.ai_menu_generator import ai_menu_generator
from app.services.rag_service import rag_service
from app.services.ai_budget_guard import ai_budget_guard, BudgetExceeded

logger = logging.getLogger(__name__)


def _parse_currency_query(query: str, rates: dict) -> Optional[dict]:
    """Deterministically parse a currency-conversion query into a structured
    (amount, from_code, to_code) triple — WCR-CURR-01, 2026-08-18.

    Root incident: asking the LLM to do two-step cross-rate arithmetic (e.g.
    IDR -> USD -> AUD, since the live rates table is USD-based) in free text
    is unreliable. Live-reproduced: "100000 IDR to AUD" returned "1.4117 AUD"
    — the raw USD->AUD exchange rate itself, not the converted amount (the
    mathematically correct answer is 7.8896 AUD). LLMs doing unassisted
    multi-step arithmetic is a known weak spot; the fix is to never let the
    model do the math at all for the cases we can confidently parse.

    Only returns a result for a confident match: "<amount> <CODE> [to <CODE>]"
    where both currency codes are present in the live `rates` dict (fetched
    fresh from open.er-api.com, USD-based so rates["USD"] == 1.0 always).
    Returns None for anything else (named currencies like "Euro", ambiguous
    phrasing, greetings, missing/invalid rates) — callers MUST fall back to
    the existing LLM path in that case; this function never guesses.
    """
    if not rates or not query:
        return None
    q = query.strip()

    # Pattern 1: both currencies explicit — "100000 IDR to AUD", "100 usd in idr"
    m = re.search(
        r'(-?[\d,]+\.?\d*)\s*([A-Za-z]{3})\s*(?:to|in|as|into|=>|->)\s*([A-Za-z]{3})\b',
        q, re.IGNORECASE,
    )
    if m:
        amount_str, from_code, to_code = m.group(1), m.group(2).upper(), m.group(3).upper()
    else:
        # Pattern 2: single currency, no target — DEFAULT rule converts to IDR.
        m2 = re.search(r'(-?[\d,]+\.?\d*)\s*([A-Za-z]{3})\b', q, re.IGNORECASE)
        if not m2:
            return None
        amount_str, from_code = m2.group(1), m2.group(2).upper()
        to_code = "IDR"
        if from_code == "IDR":
            # "100000 IDR" with no explicit target is ambiguous (the DEFAULT
            # rule only covers foreign->IDR) — let the LLM handle it.
            return None

    try:
        amount = float(amount_str.replace(",", ""))
    except ValueError:
        return None
    if amount <= 0:
        return None
    if from_code == to_code:
        return None
    if from_code not in rates or to_code not in rates:
        return None  # unrecognised/named currency — let the LLM handle it

    converted = amount / rates[from_code] * rates[to_code]
    return {"amount": amount, "from_code": from_code, "to_code": to_code, "converted": converted}


def _format_currency_result(parsed: dict) -> str:
    """Format a _parse_currency_query() result as the standard one-line
    response — matches the persona's "no extra content" rule (2026-08-10):
    the number and nothing else. IDR shows as a thousands-separated integer
    (no decimals, standard for Rupiah); other currencies show 2 decimals."""
    amount = parsed["amount"]
    amount_fmt = f"{amount:,.0f}" if amount == int(amount) else f"{amount:,.2f}"
    to_code = parsed["to_code"]
    converted = parsed["converted"]
    converted_fmt = f"{converted:,.0f}" if to_code == "IDR" else f"{converted:,.2f}"
    return f"{amount_fmt} {parsed['from_code']} = {converted_fmt} {to_code}"


async def _search_bali_events(query: str) -> str:
    """Real, live web search for current Bali events via OpenAI's built-in
    web_search tool (Responses API) — WCR-EVCAL-03, the "independent research"
    half of the Event Calendar Hybrid AI Results spec (Clay/Adam, 2026-08-18).

    Deliberately NOT the model's pretrained/training-data recall — that risks
    exactly the fabricated dates/prices/venues the spec forbids. A real search
    call returns real results with citation URLs, which the caller must use
    to satisfy "provide the source/link where appropriate" for anything drawn
    from this function's output.

    Non-fatal: any failure (tool unsupported, network, rate limit, etc.)
    returns "" so the curated sheet data alone still drives the response —
    this must never block or crash the Event Calendar flow.
    """
    try:
        resp = await client.responses.create(
            model=CHAT_TYPE_MODELS.get("event-calender", "gpt-4o-mini"),
            tools=[{"type": "web_search"}],
            input=(
                "Find CURRENT, REAL, VERIFIABLE events happening in Bali, Indonesia "
                f"relevant to this guest request: {query}\n\n"
                "For each event include: name, date, time (if known), location, and "
                "a one-line description. Only include events you can actually verify "
                "from real search results — do NOT guess, estimate, or invent any "
                "event, date, price, venue, or ticketing detail you are not certain "
                "of from the search results themselves. If you cannot find enough "
                "genuinely relevant, current events, say so plainly instead of "
                "padding the list."
            ),
        )
        text = (getattr(resp, "output_text", None) or "").strip()
        if not text:
            return ""

        # Best-effort citation extraction — the text alone is still useful if
        # this fails, so any error here must never discard `text`.
        sources = []
        try:
            for item in getattr(resp, "output", None) or []:
                for content in getattr(item, "content", None) or []:
                    for ann in getattr(content, "annotations", None) or []:
                        url = getattr(ann, "url", None)
                        if url and url not in sources:
                            sources.append(url)
        except Exception as _ann_err:
            logger.warning(f"Event search citation extraction failed (non-fatal): {_ann_err}")

        block = (
            "--- ADDITIONALLY RESEARCHED EVENTS (real web search results — use ONLY "
            "to fill gaps not already covered by the curated data above; if you use "
            "any of these, cite the source link; never treat this section as more "
            "authoritative than the curated data) ---\n"
            f"{text}"
        )
        if sources:
            block += "\n\nSource links found:\n" + "\n".join(f"- {s}" for s in sources)
        return block
    except Exception as e:
        logger.warning(f"Event search failed (non-fatal, falling back to curated data only): {e}")
        return ""


# Model routing — use gpt-4o only where quality is critical.
# Utility/lookup tabs use gpt-4o-mini (94% cheaper, no quality loss for these tasks).
CHAT_TYPE_MODELS = {
    # Premium — guest-facing concierge, complex reasoning
    "general":              "gpt-4o",
    "plan-my-trip":         "gpt-4o",
    "recommendations":      "gpt-4o",
    # Economy — translation, currency, simple lookups
    "voice-translator":     "gpt-4o-mini",
    "currency-converter":   "gpt-4o-mini",
    "what-to-do":           "gpt-4o-mini",
    "things-to-do-in-bali": "gpt-4o-mini",
    "event-calender":       "gpt-4o-mini",
    "local-cuisine":        "gpt-4o-mini",
    "passport-submission":  "gpt-4o-mini",
    "maintenance-issue":    "gpt-4o-mini",
}

# Specialized System Personas for different Chat Types
PERSONAS = {
    "recommendations-interactive": """
        You are GINI Bali's local expert for website-specific recommendations.
        Mission: The guest just selected a specific recommendation category (like a restaurant area).
        Your focus is STRICTLY on providing recommendations from our website/internal DB.
        If they ask for something beyond this, or generic requests not strictly related to our website recommendations, kindly reply:
        "Please choose the menu for other options."
        FORMAT RULES:
        - Keep responses concise and WhatsApp-friendly.
        - NEVER make up places that aren't in the provided KNOWLEDGE BASE or INTERNAL DB.
    """,
    "what-to-do": """
        You are GINI Bali’s personal activity guide — warm, enthusiastic, and genuinely helpful.
        STRICT INTERACTIVE BEHAVIOUR:
        - If the guest’s first message is a greeting ("Hi", "Hello", "Hey", etc.) OR a generic opener, IMMEDIATELY ask them about their mood/interest today.
          Use this exact style: "What kind of day are you after today? 🌴 Adventure, culture, relaxation, food, or something else?" — do NOT tell stories, do NOT ramble.
        - Once they share a mood/interest, acknowledge it warmly in one sentence, then ask ONE focused follow-up question.
          Examples: "Are you with family, a partner, or going solo?", "How much time do you have today?", "Which area of Bali are you staying in?"
        - Only after 1–2 follow-ups, suggest 2–3 SPECIFIC, well-matched activities with names and brief descriptions.
        - Always end each response with an engaging question or offer to book through GINI Bali.
        - NEVER dump a long list on first response. Build up through conversation.
        - NEVER tell stories, fictional narratives, or lengthy background — always stay practical and activity-focused.
        FORMAT: Short paragraphs, *bold* activity names, WhatsApp-friendly. Always end with a question.
    """,
    "things-to-do-in-bali": """
        You are GINI Bali’s Adventure & Exploration Guide for Bali.
        Mission: Provide a COMPLETE, COMPREHENSIVE guide to things to do in Bali. Cover ALL major categories — do not limit yourself to a short list.
        CONTENT: Include beaches & water sports, cultural landmarks & temples, adventure activities, local experiences, nightlife, day trips, family activities, wellness & spas, shopping, unique hidden gems. Give specific names, locations, and practical tips for each.
        FORMAT RULES:
        - Use numbered sections with *bold* section headers.
        - Under each section, list specific activities with 1–2 lines of detail each.
        - No tables. WhatsApp-friendly text only.
        - Be as thorough and informative as a professional travel guide.
    """,
    "plan-my-trip": """
        You are GINI Bali — AI Travel Planner for Bali.
        Your style: proactive, enthusiastic, and structured. You ask smart questions to build a personalised itinerary.
        BEHAVIOUR:
        - When a new session starts, immediately ask the guest about their arrival date, trip duration, group size, and interests (adventure/culture/relaxation/food/nightlife). Ask ONE question at a time.
        - Once you have enough info, suggest a day-by-day itinerary with specific GINI Bali services they can book.
        - Mention relevant promotions or discounts from context when available.
        - Always end each message with a clear next question or action.
        FORMAT: Use WhatsApp-friendly formatting — bold headers (*text*), numbered lists, no markdown tables.
    """,
    "event-calender": """
        You are GINI Bali's friendly Events Guide — specific, engaging, and genuinely helpful.
        STRICT INTERACTIVE BEHAVIOUR:
        - FIRST CHECK the conversation history. If the guest has NOT yet shared their stay dates, you MUST ask before listing events:
          "When are you visiting Bali? Just share your arrival and departure dates and I'll show you exactly what's happening during your stay! 🎉"
        - Once you know their dates, show ONLY 3–5 events relevant to that specific window with date, location, and what to expect.
        - If the query includes a specific date range (e.g. "this week (20 March 2026 to 26 March 2026)"), use those exact dates.
        - After listing events, ask a smart follow-up: what type do they prefer — cultural ceremonies, music/parties, markets, wellness, or nightlife?
        - Keep building the conversation. Suggest related bookings through GINI Bali when relevant.
        - NEVER dump a massive event list. Keep it focused and conversational.
        FORMAT: Numbered list, *bold* event name, 📅 date, 📍 location. Max 5 events per response, then follow up with a question.

        HYBRID AI RESULTS — SOURCING RULES (Code of Conduct, Clay/Adam, 2026-08-18):
        You will be given TWO context blocks above: "REAL EVENT CALENDAR DATA" (the
        curated source) and, when present, "ADDITIONALLY RESEARCHED EVENTS" (live
        web search results). Follow this exactly:
        1. PRIORITIZE the curated Event Calendar data as your primary source.
        2. Only pull from the researched-events block to fill a genuine gap — e.g.
           the curated data has too few events matching the guest's dates/interests.
        3. Combine both into ONE list, never listing the same event twice.
        4. NEVER fabricate an event, date, time, price, venue, or ticketing detail.
           Every specific fact you state must come from one of the two context
           blocks above — not from your own general knowledge, and not guessed.
        5. Only include researched events that are relevant, current, and that the
           search results actually support — do not include anything uncertain.
        6. When you use ANY event from the researched block, include its source
           link right after that event (e.g. "More info: <url>").
        7. If NEITHER source has anything genuinely relevant for the guest's ask,
           say so plainly — e.g. "I don't have verified events matching that yet —
           want me to suggest some general things to do instead?" — never invent
           a plausible-sounding event to avoid an empty answer.
    """,
    "local-cuisine": """
        You are GINI Bali's passionate food guide — enthusiastic, knowledgeable, and interactive.
        STRICT INTERACTIVE BEHAVIOUR:
        - NEVER dump a full food guide upfront. Always start by understanding their mood/preference.
        - If the guest has just selected a food mood (adventurous/laid-back/safe/seafood), acknowledge it warmly and ask ONE smart follow-up:
          e.g. "Which area of Bali are you in?" or "Are you looking for street food or a sit-down restaurant?" or "Any dietary needs — vegetarian, vegan, halal?"
        - After 1–2 follow-ups, give 2–3 SPECIFIC recommendations: dish name, the spot/warung, and one line on what makes it special.
        - Always end with a question or offer to book a cooking class or food tour through GINI Bali.
        - NEVER list more than 3 recommendations at once. Keep it focused.
        FORMAT: Short, punchy. *Bold* dish/place names. Friendly tone. Always end with a question.
    """,
    "currency-converter": """
        ⚠️ ABSOLUTE FORMATTING RULE — NEVER BREAK THIS UNDER ANY CIRCUMSTANCE:
        Do NOT use LaTeX. Do NOT write \[, \], \text{}, \frac{}, \times, \left, \right, or ANY math markup.
        Write ONLY plain text. Example of correct output: "3,000 INR ÷ 93.18 = 32.19 USD × 17,004 = IDR 547,204"
        Example of FORBIDDEN output: \[ 3{,}000 \text{ INR } \times \frac{1}{93.18} \]
        If you use LaTeX syntax, you have FAILED. Always use plain arithmetic symbols: +, -, ×, ÷, =

        You are the Global Currency Assistant for tourists visiting Bali, Indonesia.

        LIVE RATES RULE: The user's message begins with [LIVE_RATES: ...]. Use ONLY these rates. Never use training-data rates — they are outdated.

        DEFAULT: Unless the user specifies a target currency, ALWAYS convert TO Indonesian Rupiah (IDR).
        Example: "100 USD" → respond "100 USD = IDR 1,700,400" (one line, plain text, no LaTeX).

        GREETING: If the message is a greeting or has no amount, respond:
        "Hi! I'm your Bali currency converter. Tell me an amount and currency — e.g. '100 USD' or '50 EUR' — and I'll give you the IDR equivalent instantly!"
        Do NOT list all rates on greeting.

        For every conversion:
        1. Find source currency and amount from the user message.
        2. If no target currency given, use IDR.
        3. Extract rate from [LIVE_RATES] block.
        4. Calculate and show result as plain text (e.g., "100 USD = IDR 1,700,400").

        NO EXTRA CONTENT (2026-08-10): The guest asked for a number — give them the number
        and nothing else. Do NOT add a local tip, a recommendation, a follow-up question,
        or any other commentary after the conversion result.
        ✅ CORRECT: "100 USD = IDR 1,700,400"
        ❌ WRONG: "100 USD = IDR 1,700,400. Pro tip: always double-check the rate at your hotel!"
        ❌ WRONG: "100 USD = IDR 1,700,400 — enough for a nice dinner in Seminyak!"

        Be friendly, concise, and use plain readable text only — no markdown tables, no LaTeX, no code blocks.
    """,
    "voice-translator": """
        You are the GINI Bali Translator. You have exactly ONE job: translate whatever
        the user sends. There is no second mode. You never answer questions, never give
        advice, never quote prices, never mention villas, WiFi, checkout, staff, or
        GINI Bali services, and never redirect the user anywhere. Translation is your
        only output — always, with no exceptions.

        This applies to EVERYTHING the user sends: phrases, sentences, words, questions,
        shopping phrases, greetings, pleasantries, questions about wifi/pool/checkout,
        anything a tourist might say to a local shopkeeper/driver/host, and anything a
        guest might ask about their villa or stay. If it looks like a question, translate
        the question AS TEXT — do not attempt to answer it.

        SUPPORTED LANGUAGES:
        English, Hindi, Malayalam, Russian, French, Spanish, Italian, Mandarin, Japanese, Korean, Thai
        — and any other detectable language (e.g. from voice transcription).

        STRICT OUTPUT FORMAT — NEVER DEVIATE:
        [Original] → [Translation] (Pronunciation guide when translating TO Indonesian)
        ✅ CORRECT: "I like this bag" → "Saya suka tas ini" (Sah-yah soo-kah tahs ee-nee)
        ✅ CORRECT: "What is the price of this bag?" → "Berapa harga tas ini?" (Buh-rah-pah har-gah tahs ee-nee)
        ✅ CORRECT: "What is the wifi password?" → "Apa kata sandi wifi-nya?" (Ah-pah kah-tah sahn-dee wifi-nyah)
        ❌ WRONG: "In Indonesian, you would say 'Saya suka tas ini' to express 'I like this bag.' It's always..."
        ❌ WRONG: Any sentence starting with "In [language]..." or "You would say..."
        ❌ WRONG: Answering the question instead of translating it (e.g. giving an actual
                  price, quoting a real wifi password, explaining "I don't have that info",
                  or saying "check with your villa manager")
        Do NOT explain. Do NOT add filler. Do NOT use prose. Output ONLY the arrow line.
        For multi-phrase inputs, output one arrow line per phrase — nothing else.

        DEFAULT RULE — Non-Indonesian input → Indonesian:
        Translate to Indonesian. Always include pronunciation guide.
        Example: "Thank you very much" → "Terima kasih banyak" (Tuh-ree-mah kah-sih bah-nyak)

        SMART FLIP RULE — Indonesian input + session language in history:
        Check CONVERSATION HISTORY for the non-Indonesian language the user last used.
        If found → translate the Indonesian input TO that session language.
        Example (after Japanese session): "Satu, dua, tiga" → "一、二、三" (Ichi, ni, san)

        FALLBACK RULE — Indonesian input + NO session language in history:
        Translate to English (default for first-time Indonesian input).
        Example: "Selamat pagi" → "Good morning"

        VOICE INPUT RULE:
        If the input starts with "[VOICE] ", it is a voice transcription. Format as:
        🎤 Heard: [transcribed text]
        [transcribed text] → [translation]

        NO EXCEPTIONS: Every single input gets translated per the rules above — no
        classification, no judgment call, no second mode. If you are ever unsure what
        to do with an input, translate it. Never break character to answer, explain,
        recommend, or redirect.
    """,
    "passport-submission": """
        Security Assistant.
        Help with passport uploads. Guide guests to the dedicated upload form shown
        above this chat (Full Name field + upload box + Securely Submit Passport
        button). Never mention a paperclip icon or any chat-input attachment — there
        is no attachment control in this chat; passports are submitted only through
        that form.
    """,
    "maintenance-issue": """
        You are the GINI Bali Maintenance Concierge.
        Help villa guests report maintenance issues (broken AC, plumbing, electrical, pool, furniture etc.).
        Collect: type of issue, location in villa, urgency (low/medium/high), and any photos if available.
        Always reassure the guest the team will respond promptly. Keep responses short and professional.
    """,
    "general": """
        Premium concierge for Bali villa guests.
        Professional, excited, high-end.
        You have been provided deep context from the Archive and Price Diff tabs. If a user asks about a service you cannot find in the active directory, check the Archive list provided in your context. If it's still not there, intelligently browse your own general knowledge to answer.

        CRITICAL RULES:
        - NEVER tell a user that a service is "booked" or that an "email has been sent" for booking.
        - If a user wants to book, guide them to use the interactive "Book" or "Options" buttons that appear in the chat.
        - If you don't see buttons yet, ask for their preferred date and time so the system can suggest options.

        COMMUNICATION FORMAT (2026-08-07):
        - Keep replies short — 2-4 short paragraphs or a tight bulleted list. Never one unbroken wall of text.
        - For big, multi-part asks (e.g. "plan my whole trip"), do NOT answer everything at once. Acknowledge warmly in one sentence, then ask ONE focused follow-up question to narrow it down. Build the answer across the conversation.
        - Use 1-3 emojis naturally where they fit — never forced, never more than a few per message.
        - Use *bold* for activity/service names and short bullets for more than one option.
        - Every reply ends with ONE clear next step — a specific question, or the exact button/menu name to use. Never end on a flat statement with nothing to do next.
        - If the guest says "I don't understand" or repeats a question, do NOT restate your previous message in different words — simplify to the single most useful fact or action.
        EXAMPLE:
        ✅ Guest: "I have 18 days, want nature, family time, and a co-working space." → "18 days sounds amazing! 🌴 What matters most for your first few days — adventure, relaxing with family, or getting settled in first?"
        ❌ WRONG: a long single-paragraph itinerary covering everything at once with no question at the end.
    """
}

class ConciergeAI:
    """
    CONTAINERIZED AI MODULE.
    Isolation Principle: Logic is separated from Routing.
    Local First Principle: Sheets -> RAG -> OpenAI.
    """
    def __init__(self, service_index: str = "ai-data"):
        self.service_index = service_index

    async def get_rag_context(self, query: str, chat_type: str = "general", villa_code: str = "WEB_VILLA_01") -> str:
        """Proxies to the unified RAG service"""
        return await rag_service.get_rag_context(query, chat_type, villa_code)

    def get_sheet_context(self) -> str:
        """INTERNAL DB FETCH (Priority 1)"""
        context = ""
        try:
            # 1. AI Data Sheet (Core context)
            from app.utils.formatters import clean_price_string
            if cache.get("ai_data_df") is not None and not cache["ai_data_df"].empty:
                context += "--- ACTIVE SERVICES ---\n"
                # Use all available active services for accuracy (usually < 200 items)
                for _, row in cache["ai_data_df"].iterrows():
                    raw_price = str(row.get('Price (Service Item Button)', '')).strip()
                    clean_price = clean_price_string(raw_price)
                    context += f"- {row.get('Service Item')}: {row.get('Service Item Description')} Price: {clean_price}\n"
            
            # 2. Archive Data Sheet (Deep context)
            if cache.get("archive_df") is not None and not cache["archive_df"].empty:
                context += "\n--- ARCHIVE DATA (Rentals & Legacy) ---\n"
                arch_lines = []
                # Increase visibility into archive for better rental identification
                for _, row in cache["archive_df"].head(300).iterrows():
                    valid = [f"{col}:{val}" for col, val in row.items() if pd.notna(val) and str(val).strip()]
                    if valid: arch_lines.append(" | ".join(valid))
                context += "\n".join(arch_lines) + "\n"

            # 3. Price Diff / Pricing Tiers
            for sheet_name, lbl in [("price_diff_df", "PRICE DIFF"), ("price_diff_sp_df", "PRICE DIFF SP")]:
                if cache.get(sheet_name) is not None and not cache[sheet_name].empty:
                    context += f"\n--- {lbl} ---\n"
                    lines = []
                    for _, row in cache[sheet_name].head(150).iterrows():
                        valid = [f"{col}:{val}" for col, val in row.items() if pd.notna(val) and str(val).strip()]
                        if valid: lines.append(" | ".join(valid))
                    context += "\n".join(lines) + "\n"
                    
            # 4. Platform Design
            if cache.get("platform_design_df") is not None and not cache["platform_design_df"].empty:
                context += "\n--- PLATFORM DESIGN ---\n"
                p_lines = []
                for _, row in cache["platform_design_df"].head(50).iterrows():
                    valid = [f"{col}:{val}" for col, val in row.items() if pd.notna(val) and str(val).strip()]
                    if valid: p_lines.append(" | ".join(valid))
                context += "\n".join(p_lines) + "\n"

        except Exception as e:
            logger.error(f"Sheet Context Compilation Error: {e}")
            
        # Cap at 5,000 chars — RAG context covers the gap for specific queries
        return context[:5000]

    async def process_query(self, query: str, user_id: str, chat_type: str, language: str, villa_code: str = "WEB_VILLA_01") -> Dict[str, Any]:
        """
        Process user query based on chat_type.
        Each chat type has an isolated path to prevent regression.
        Service booking detection ONLY runs for order-service modes.
        """
        try:
            from app.services.order_summary import get_active_order_context
            print(f"DEBUG: Processing query='{query}' chat_type='{chat_type}' user='{user_id}'")
            # Route to cheaper model for utility tabs — gpt-4o only for concierge/planning
            _chat_model = CHAT_TYPE_MODELS.get(chat_type, settings.OPENAI_MODEL_NAME)
            history = get_conversation_history(user_id)
            conv = trim_history(history + [{"role": "user", "content": query}])
            formatted_history = "\n".join([f"{m['role'].capitalize()}: {m['content']}" for m in conv])

            # ─── HIGH-PRIORITY INTERCEPTS ──────────────────────────────────────────
            # 1. Language Lesson Loop (Mirrors WhatsApp flow)
            # Checked before service check to prevent service-selection hijacking
            from app.utils.language_lesson_utils import get_web_lesson_payload
            _q_lower = query.lower()
            
            # Start/Continue Lesson
            if "local language lesson" in _q_lower:
                payload = get_web_lesson_payload(index=0)
                resp = f"{payload['response']}|NAV_OPTIONS|{json.dumps(payload['nav_options'])}"
                save_message(user_id, "assistant", resp)
                return {"response": resp}
            
            # Next Word Cycle
            if _q_lower.startswith("language_next_"):
                try:
                    idx = int(_q_lower.split("_")[-1])
                    payload = get_web_lesson_payload(index=idx)
                    resp = f"{payload['response']}|NAV_OPTIONS|{json.dumps(payload['nav_options'])}"
                    save_message(user_id, "assistant", resp)
                    return {"response": resp}
                except:
                    pass
            
            # Stop Lesson
            if _q_lower == "language_stop":
                resp = "Great effort today! 🌟 I'll be here if you want to learn more later. What else can I help you with?"
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            # ─── SERVICE BOOKING INTERCEPT (Dynamic Mapping) ────────────────────────
            # Intercept service requests across all chat types for consistency
            try:
                # Only run for modes that actually allow ordering
                if chat_type in ["general", "order-service", "plan-my-trip", "what-to-do", "things-to-do-in-bali", "local-cuisine", "recommendations"]:
                    service_check = await ai_menu_generator.intelligent_service_check(query)
                    if service_check.get("is_service_request") and service_check.get("we_offer_it"):
                        menu = await self._handle_booking(query, user_id, language, service_check, villa_code)
                        if menu:
                            return menu
                    # WCR-23 (additive — mirrors the WhatsApp-side fix in
                    # whatsapp_ai_prompt.py, does not alter the block above):
                    # deterministic backstop for intelligent_service_check() false
                    # negatives. That call is a live AI classification and is not
                    # 100% reliable — it can say we_offer_it=False for a service
                    # explicitly on its own AVAILABLE SERVICES list. _handle_booking()
                    # already calls the reliable, deterministic detect_service_intent()
                    # keyword matcher internally, but was previously only ever reached
                    # when the AI had ALREADY said yes. This runs only in the
                    # complementary case and, if the deterministic matcher confirms a
                    # real match, calls _handle_booking() with a corrected check dict.
                    elif not (service_check.get("is_service_request") and service_check.get("we_offer_it")):
                        _wcr23_backstop = ai_menu_generator.detect_service_intent(query)
                        if _wcr23_backstop:
                            _wcr23_check = dict(service_check)
                            _wcr23_check["is_service_request"] = True
                            _wcr23_check["we_offer_it"] = True
                            _wcr23_check["matched_service"] = _wcr23_backstop.get("subcategory")
                            menu = await self._handle_booking(query, user_id, language, _wcr23_check, villa_code)
                            if menu:
                                return menu
            except Exception as e:
                logger.error(f"Service Check Error: {e}")

            # ─── SERVICE CATALOG RECOMMENDATION ────────────────────────────────────
            # When guest asks what services are available — only answer from the sheet.
            _CATALOG_PHRASES = [
                "what services", "services do you", "services you offer",
                "available services", "services available", "what can i book",
                "what can we book", "what do you offer", "what do you have",
                "list of services", "all services", "show me your service",
                "what can you recommend", "recommend some service",
                "what activities do you", "what experiences do you",
                "what options do you", "what can i order",
                # Natural "can I get X / where do I order X" phrasing — these
                # must also pull from the services sheet so the concierge only
                # offers what GINI Bali actually provides (Point 18).
                "where can i order", "where can i get", "where can i book",
                "where do i order", "where to order", "where to get",
                "can i order", "can i get", "can i book", "do you have",
                "do you offer", "is there any", "are there any", "looking for",
                "i want to book", "i want to order", "how do i book",
                "how can i book", "how can i order", "any recommendation",
                "recommend", "suggestion", "suggest",
            ]
            # Recommendations chat always answers from the sheet; general/order
            # chat answers from the sheet when the message looks like a service
            # request (Point 18 — no more generic replies to "where can I get X").
            _wants_catalog = (
                chat_type == "recommendations"
                or (chat_type in ["general", "order-service"] and any(p in _q_lower for p in _CATALOG_PHRASES))
            )
            if _wants_catalog:
                try:
                    from app.services.menu_services import get_service_catalog_context
                    _catalog_ctx = await get_service_catalog_context(villa_code=villa_code)
                    if _catalog_ctx:
                        _catalog_persona = (
                            "You are GINI Bali's service concierge. "
                            "When listing or recommending services, you MUST use ONLY the "
                            "AVAILABLE EASYBALI SERVICES list provided — never suggest or mention "
                            "any service not in that list. Group them by category. "
                            "Include the price for each service. Be warm, concise, and inviting."
                        )
                        resp = await self._call_openai(
                            query, _catalog_persona, _catalog_ctx, "",
                            language, formatted_history, villa_code, model=_chat_model
                        )
                        save_message(user_id, "user", query)
                        save_message(user_id, "assistant", resp)
                        return {"response": resp}
                except Exception as _cat_err:
                    logger.warning(f"Service catalog recommendation failed (non-fatal): {_cat_err}")

            # ─── DISCOUNTS & PROMOTIONS: SHOW THE REAL MENU, NOT AI PROSE ──────────
            # Root cause fixed (2026-08-15, Adam/Clay live report): free-text D&P
            # questions ("discounts and promotions") first got zero grounding (the
            # AI incorrectly claimed no promotions existed), then — after adding
            # grounding — got an AI TEXT description of the promos instead of the
            # actual interactive Discounts & Promotions view. Every other
            # menu-equivalent free-text query on this platform triggers the real
            # UI, not prose (see _wants_catalog's SERVICES_DATA| sentinel above,
            # and the WhatsApp-side send_dnp_flow_message mirror of this same fix).
            # DNP_MENU| is a sentinel: chat.jsx intercepts it, shows the short
            # intro text as a bot message, and switches activeTab to
            # 'discounts_promotions' so the guest sees the live DNPView with
            # real, tappable category tiles — not a chat bubble imitating it.
            _DNP_PHRASES = [
                "discount", "discounts", "promotion", "promotions", "promo",
                "promos", "deal", "deals", "voucher", "vouchers", "offer",
                "offers", "special offer", "any discount", "any promo",
            ]
            _wants_dnp = chat_type in ["general", "order-service"] and any(p in _q_lower for p in _DNP_PHRASES)
            if _wants_dnp:
                try:
                    from app.services.menu_services import get_dnp_context
                    _dnp_ctx = await get_dnp_context()
                    if _dnp_ctx:
                        _dnp_intro = "Great news! 🎉 We have active discounts and promotions for you — take a look:"
                        resp = f"DNP_MENU|{_dnp_intro}"
                        save_message(user_id, "user", query)
                        save_message(user_id, "assistant", resp)
                        return {"response": resp}
                    # _dnp_ctx == "" means genuinely no active promotions right now —
                    # fall through to the normal persona so the AI can say so honestly
                    # without a hardcoded/stale claim either way.
                except Exception as _dnp_err:
                    logger.warning(f"D&P grounding failed (non-fatal): {_dnp_err}")

            # ─── AMENITY REQUEST (QR villa guests only) ────────────────────────────
            # AMR-VG1/VG2 (mirrored from whatsapp_ai_prompt.py, 2026-08-26): generic
            # verbs ("extra", "refresh") and plain substring matching caused false
            # positives on real questions ("nice massage" matched "ice", "order
            # services" matched "ice" inside serv[ice]s) — misrouting the guest away
            # from booking before they ever reached it. Fixed to match the already-
            # hardened WhatsApp implementation: concrete items only, word-boundary
            # regex, and a skip-phrase guard for ordering language.
            _amenity_kws = [
                "towel", "towels", "coffee", "tea", "water", "ice", "toiletries",
                "shampoo", "soap", "conditioner", "blanket", "pillow", "toilet paper",
                "tissue", "amenities", "amenity", "toothbrush", "toothpaste",
                "razor", "slippers", "hangers", "iron", "hair dryer", "batteries",
                "refill",
            ]
            _AMENITY_SKIP_PHRASES = {"order service", "order services", "browse service", "book a service"}
            if (
                villa_code and villa_code not in ("WEB_VILLA_01", "")
                and chat_type != "voice-translator"
                and not any(p in _q_lower for p in _AMENITY_SKIP_PHRASES)
                and any(re.search(r'\b' + re.escape(kw) + r'\b', _q_lower) for kw in _amenity_kws)
                and len(query.strip().split()) >= 2
            ):
                try:
                    from app.db.session import amenities_collection as _am_col
                    from datetime import datetime as _dt_am
                    _now_am = _dt_am.utcnow()
                    _item = next((kw.title() for kw in _amenity_kws if re.search(r'\b' + re.escape(kw) + r'\b', _q_lower)), "General")
                    await _am_col.insert_one({
                        "sender_id": user_id,
                        "villa_code": villa_code,
                        "request_description": query,
                        "item_type": _item,
                        "quantity": 1,
                        "source": "web",
                        "urgency": "normal",
                        "status": "open",
                        "history": [{"status": "open", "timestamp": _now_am, "note": "Request via web chat"}],
                        "created_at": _now_am,
                        "updated_at": _now_am,
                    })
                    _am_resp = (
                        f"🛎️ Your request for **{_item}** has been logged! "
                        f"Our team will deliver it to your room shortly.\n\n"
                        f"You'll be notified here once it's on the way. Is there anything else I can help you with? 😊"
                    )
                    save_message(user_id, "user", query)
                    save_message(user_id, "assistant", _am_resp)
                    return {"response": _am_resp}
                except Exception as _am_web_err:
                    logger.warning(f"Web amenity save failed (non-fatal): {_am_web_err}")

            # ─── PASSPORT SUBMISSION ───────────────────────────────────────────────
            if chat_type == "passport-submission":
                if _q_lower in ["hi", "hello", "hi there"]:
                    return self._passport_hi(user_id, language)
                # Let general AI handle follow-up, but keep persona focused
                resp = await self._call_openai(query, PERSONAS["passport-submission"], "", "", language, formatted_history, villa_code, model=_chat_model)
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            # ─── MAINTENANCE ISSUE ─────────────────────────────────────────────────
            if chat_type == "maintenance-issue":
                if _q_lower in ["hi", "hello", "hi there"]:
                    txt = (
                        "🛠️ Hi! I'm here to help log your maintenance issue.\n\n"
                        "Please describe the problem (e.g. broken AC, leaking tap, power outage). "
                        "I'll make sure the villa team is notified right away!"
                    )
                    save_message(user_id, "assistant", txt)
                    return {"response": txt}
                resp = await self._call_openai(query, PERSONAS["maintenance-issue"], "", "", language, formatted_history, villa_code, model=_chat_model)
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", resp)
                # Save to issues DB so it's visible in Maintenance Issues dashboard
                _issue_kw = ["broken", "not working", "issue", "problem", "leaking", "leak",
                             "complain", "repair", "fix", "stuck", "noise", "smell", "dirty",
                             "ac", "wifi", "tv", "shower", "toilet", "door", "window", "lock",
                             "water", "electric", "power", "pool", "bed", "sofa", "fridge"]
                if len(query.strip()) > 10 and any(kw in _q_lower for kw in _issue_kw):
                    try:
                        from app.db.session import db as _issue_db
                        from datetime import datetime as _dt
                        await _issue_db["issues"].insert_one({
                            "sender_id": user_id,
                            "customer_id": None,
                            "villa_code": villa_code or "WEB_VILLA_01",
                            "description": query,
                            "media_type": "text",
                            "status": "open",
                            "source": "web",
                            "timestamp": _dt.utcnow()
                        })
                    except Exception as _ie:
                        logger.error(f"Failed to save web maintenance issue to DB: {_ie}")
                return {"response": resp}

            if chat_type == "voice-translator":
                # Hardcode greeting — prevents language lesson bleed-through and guarantees a translator-only intro
                if _q_lower in {"hi", "hello", "hey", "hii", "start", "hi there"}:
                    txt = (
                        "Halo! Saya Penerjemah GINI Bali Anda. Ketik atau ucapkan frasa apa saja dan saya akan menerjemahkannya untuk Anda! 🌐"
                        if language == "ID" else
                        "Hi! I'm your GINI Bali Translator. Say or type anything and I'll translate it for you! 🌐"
                    )
                    save_message(user_id, "assistant", txt)
                    return {"response": txt}
                # Translator-only: no villa context, no FAQ/knowledge-base injection, no
                # generic concierge RULES (grounding/out-of-scope/fallback) — those rules
                # were overriding the persona's translate-everything instruction and
                # causing shopping/villa questions to be "answered" instead of translated.
                resp = await self._call_openai(query, PERSONAS["voice-translator"], "", "", language, formatted_history, villa_code, inject_villa_context=False, model=_chat_model, translator_mode=True)
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            # ─── CURRENCY CONVERTER ────────────────────────────────────────────────
            if chat_type == "currency-converter":
                _original_query = query
                _rates_for_calc: dict = {}
                try:
                    import httpx as _httpx
                    async with _httpx.AsyncClient(timeout=5.0) as _hc:
                        _r = await _hc.get("https://open.er-api.com/v6/latest/USD")
                        if _r.status_code == 200:
                            _rates = _r.json().get("rates", {})
                            _rates_for_calc = _rates
                            _rate_str = "Live exchange rates (base USD): " + " | ".join(
                                f"1 USD = {v:,.4f} {k}" for k, v in sorted(_rates.items())
                            )
                            query = f"[LIVE_RATES: {_rate_str}]\nUSER QUERY: {query}"
                except Exception:
                    pass

                # WCR-CURR-01: deterministic cross-rate math — see
                # _parse_currency_query()'s docstring for the root incident.
                # Parse the ORIGINAL query (before the [LIVE_RATES:...] prefix
                # is prepended above) — if it confidently matches, compute and
                # format the answer directly, no LLM arithmetic involved.
                _parsed_currency = _parse_currency_query(_original_query, _rates_for_calc)
                if _parsed_currency:
                    resp = _format_currency_result(_parsed_currency)
                    save_message(user_id, "user", _original_query)
                    save_message(user_id, "assistant", resp)
                    return {"response": resp}

                resp = await self._call_openai(query, PERSONAS["currency-converter"], "", "", language, formatted_history, villa_code, model=_chat_model)
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            # ─── PLAN MY TRIP ──────────────────────────────────────────────────────
            if chat_type == "plan-my-trip":
                rag_ctx = await self.get_rag_context(query, chat_type, villa_code)
                resp = await self._call_openai(query, PERSONAS["plan-my-trip"], "", rag_ctx, language, formatted_history, villa_code, model=_chat_model)
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            # ─── WHAT TO DO / EVENT CALENDAR / LOCAL GUIDE ────────────────────────
            # Interactive kickoff handlers — return engaging first question instead of content dump
            _initial_triggers = {"hi", "hello", "hey", "start", "hii"}
            if chat_type == "what-to-do" and _q_lower in _initial_triggers:
                buttons_data = {
                    "title": "What would you like to do today? 🌴",
                    "subtitle": "Pick what best describes your mood:",
                    "buttons": [
                        "📸 Nice photos for my Instagram",
                        "👨‍👩‍👧 Quality time with friends or family",
                        "🌺 Try a local experience",
                        "🤪 Do something crazy!"
                    ]
                }
                resp = f"QUICK_REPLY_BUTTONS|{json.dumps(buttons_data)}"
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            if chat_type == "event-calender" and _q_lower in _initial_triggers:
                resp = (
                    "Hi! I'm your GINI Bali Events Guide 🎉\n\n"
                    "To show you the most relevant events, let me ask — *when are you visiting Bali?*\n\n"
                    "Just share your arrival and departure dates and I'll pull up everything exciting happening during your stay!"
                )
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            if chat_type == "local-cuisine" and _q_lower in _initial_triggers:
                resp = (
                    "Hey foodie! 🍜 Welcome to Bali's incredible food scene!\n\n"
                    "First things first — *what are you in the mood for today?*\n\n"
                    "🔥 *Adventurous* — try something wild and local\n"
                    "😌 *Laid-back* — comfort food, easy vibes\n"
                    "🛡️ *Safe* — familiar flavours but still delicious\n"
                    "🦞 *Seafood* — fresh catches straight from Bali's waters\n\n"
                    "Just pick your vibe and I'll find the perfect spots! 🌴"
                )
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            # Inject actual date range when "this week" is in an event calendar query
            if chat_type == "event-calender" and "this week" in _q_lower:
                from datetime import datetime as _dt, timedelta as _td
                _today = _dt.now()
                _week_start = _today - _td(days=_today.weekday())
                _week_end = _week_start + _td(days=6)
                _date_range = f"{_week_start.strftime('%d %B %Y')} to {_week_end.strftime('%d %B %Y')}"
                query = query.replace("this week", f"this week ({_date_range})")

            if chat_type in ["what-to-do", "local-cuisine", "things-to-do-in-bali", "event-calender"]:
                sheet_ctx = self.get_sheet_context()
                # Inject real event calendar data from AI Material spreadsheet
                if chat_type == "event-calender":
                    try:
                        from app.services.menu_services import get_event_calendar_context
                        event_ctx = get_event_calendar_context()
                        sheet_ctx = f"--- REAL EVENT CALENDAR DATA (use this as primary source) ---\n{event_ctx}\n\n" + sheet_ctx
                    except Exception as _ec_err:
                        logger.warning(f"Could not load event calendar context: {_ec_err}")
                    # WCR-EVCAL-03: Hybrid AI Results — real web search to fill gaps
                    # the curated sheet doesn't cover, per Clay/Adam's Code of
                    # Conduct spec. Non-fatal — a search failure just means the
                    # response falls back to sheet-only, exactly as before.
                    try:
                        _search_block = await _search_bali_events(query)
                        if _search_block:
                            sheet_ctx = sheet_ctx + "\n\n" + _search_block
                    except Exception as _search_err:
                        logger.warning(f"Event search step failed (non-fatal): {_search_err}")
                rag_ctx = await self.get_rag_context(query, chat_type, villa_code)
                persona = PERSONAS.get(chat_type, PERSONAS["what-to-do"])
                _order_ctx = await get_active_order_context(user_id)
                resp = await self._call_openai(query, persona, sheet_ctx, rag_ctx, language, formatted_history, villa_code, model=_chat_model, order_context=_order_ctx)
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", resp)
                return {"response": resp}

            # General fallback with RAG
            sheet_ctx = self.get_sheet_context()
            rag_ctx = await self.get_rag_context(query, chat_type, villa_code)
            persona = PERSONAS.get(chat_type, PERSONAS["general"])
            _order_ctx = await get_active_order_context(user_id)
            resp = await self._call_openai(query, persona, sheet_ctx, rag_ctx, language, formatted_history, villa_code, order_context=_order_ctx)
            save_message(user_id, "user", query)
            save_message(user_id, "assistant", resp)
            return {"response": resp}

        except Exception as e:
            logger.error(f"Critical process_query Error: {e}")
            fallback_text = (
                "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. "
                "Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581."
            )
            return {"response": fallback_text}

    async def _call_openai(self, query: str, persona: str, sheet_ctx: str, rag_ctx: str, language: str, history: str, villa_code: str, inject_villa_context: bool = True, model: str = None, order_context: str = "", translator_mode: bool = False) -> str:
        """Centralised OpenAI call with a structured prompt.

        translator_mode: when True (voice-translator only), skips villa FAQ/knowledge-base
        injection and the generic concierge RULES block entirely. Those were causing the
        translator to "answer" shopping/villa questions instead of translating them —
        see CLAUDE.md Voice Translator invariants. Never set this for any other persona.
        """
        from app.services.menu_services import get_villa_info_by_code
        from datetime import datetime
        villa_context = ""
        villa_info = None
        if inject_villa_context:
            villa_info = await get_villa_info_by_code(villa_code)
            if villa_info:
                villa_context = f"VILLA INFO: Name: {villa_info.get('name')}, Location: {villa_info.get('location')}, Address: {villa_info.get('address')}"
                if villa_info.get('directions'):
                    villa_context += f", Directions: {villa_info.get('directions')}"

        villa_rules_context = ""
        if not translator_mode:
            # ─── Direct villa FAQ injection (MongoDB) — mirrors whatsapp_ai_prompt.py ──
            try:
                from app.db.session import db as _faq_db
                _faq_codes = [villa_code]
                if villa_code and villa_code != "WEB_VILLA_01":
                    _faq_codes.append("WEB_VILLA_01")
                _faq_col = _faq_db["villa_faqs"]
                _villa_faqs = await _faq_col.find(
                    {"villa_code": {"$in": _faq_codes}}
                ).to_list(50)
                if _villa_faqs:
                    _rules = [f"Q: {f['question']}\nA: {f['answer']}" for f in _villa_faqs]
                    villa_rules_context = (
                        "VILLA HOUSE RULES & FACTS (treat these as absolute truth — "
                        "answer questions matching any of these directly and precisely):\n\n"
                        + "\n\n".join(_rules)
                    )
            except Exception as _faq_err:
                logger.warning(f"Villa FAQ lookup failed (non-fatal): {_faq_err}")

            # ─── General Knowledge Base injection (MongoDB) — mirrors whatsapp_ai_prompt.py ──
            # Free-form behaviour/response guidelines, scoped global + villa-specific.
            # Appended to villa_rules_context so it flows into the same prompt. Non-fatal.
            try:
                from app.db.session import db as _kb_db
                _kb_codes = [villa_code]
                if villa_code and villa_code != "WEB_VILLA_01":
                    _kb_codes.append("WEB_VILLA_01")
                _kb_col = _kb_db["knowledge_base"]
                _kb_docs = await _kb_col.find({"villa_code": {"$in": _kb_codes}}).to_list(10)
                _kb_parts = [d.get("content", "").strip() for d in _kb_docs if d.get("content", "").strip()]
                if _kb_parts:
                    _kb_block = (
                        "GENERAL KNOWLEDGE & RESPONSE GUIDELINES (follow these behaviour and "
                        "response rules when replying to the guest):\n\n"
                        + "\n\n".join(_kb_parts)
                    )
                    villa_rules_context = (villa_rules_context + "\n\n" + _kb_block) if villa_rules_context else _kb_block
            except Exception as _kb_err:
                logger.warning(f"Knowledge base lookup failed (non-fatal): {_kb_err}")

        current_time = datetime.now().strftime("%A, %Y-%m-%d %H:%M")
        if inject_villa_context:
            _villa_display = villa_info.get('name') if villa_info else "the villa"
            guest_context_line = f"You are assisting a guest at {_villa_display}."
        else:
            guest_context_line = ""

        if translator_mode:
            # No grounding/out-of-scope/fallback rules — those tell the model to "answer"
            # or "redirect" when something isn't in the (empty) DB/KB context, which
            # directly overrides the persona's translate-everything instruction.
            _rules_block = (
                f"- Respond in language: {language}\n"
                "- **TRANSLATOR-ONLY MODE**: Your ONLY function is translation, exactly as "
                "specified in your PERSONA instructions above. NEVER answer questions, give "
                "advice, quote prices, mention villas/wifi/checkout/staff, or redirect the "
                "user anywhere — even if the input looks like a question. Translate the "
                "input as text; do not attempt to answer it.\n"
                "- Never say \"Not Found\" or expose technical errors to the user."
            )
        else:
            _rules_block = (
                f"- Respond in language: {language}\n"
                "- **GROUNDING RULE**: Use the provided \"INTERNAL DB\" and \"KNOWLEDGE BASE\" as your primary and mandatory sources of truth for specific services, prices, and house rules.\n"
                "- **FALLBACK RULE**: If the required information is not found in the structured data above, only then use your general LLM reasoning to provide a helpful, culturally relevant answer.\n"
                "- **NO HALLUCINATION**: Never invent services, venues, prices, or details not found in the provided data. If something is outside GINI Bali's scope, acknowledge politely and redirect to what GINI Bali can help with.\n"
                "- **OUT OF SCOPE**: If the guest asks about something GINI Bali does not offer, briefly acknowledge it and redirect with one relevant GINI Bali service suggestion.\n"
                "- **NO BOOKING OVERPROMISE** (2026-08-10): Only offer to book, arrange, or reserve an activity if it is a confirmed GINI Bali catalog service (found in the INTERNAL DB/KNOWLEDGE BASE context, or GINI Bali's known service categories). For any other activity you mention or suggest (e.g. paragliding, surfing lessons, hiking, museum visits), you may describe it, but NEVER claim you can book/arrange/reserve/confirm it — say it isn't something GINI Bali books directly and redirect to what IS bookable via Order Services. ✅ \"Paragliding sounds amazing! That's not something we book directly, but I can help arrange transport there through Order Services.\" ❌ \"I can arrange that paragliding trip for you!\" / \"Consider it booked!\"\n"
                "- **INVOICE & RECEIPT** (2026-08-14): Every guest who completes payment receives an official receipt automatically. WhatsApp guests get a downloadable receipt link right in their chat. Website guests land on an Order Confirmation page after paying, which has a Download Receipt button and can be revisited anytime (the receipt link never expires). If a guest asks where their invoice/receipt/order confirmation is, or can't find it: tell them it was sent automatically when they paid — WhatsApp guests should check their GINI Bali chat for the receipt link; website guests can reopen their Order Confirmation page. NEVER invent, guess, or fabricate an invoice URL or order number. If they still can't find it, give the support number +62 851-908-28581.\n"
                "- **ESCALATION**: If a guest is frustrated, asks to speak to a real person / manager / supervisor, or has an urgent issue you cannot resolve, always provide the GINI Bali customer support escalation number: +62 851-908-28581. Say: \"For direct support, please call or WhatsApp us at +62 851-908-28581.\"\n"
                "- Be concise, helpful, and professional.\n"
                "- Never say \"Not Found\" or expose technical errors to the user.\n"
                "- Do NOT ask for information you already have.\n"
                "- **PROACTIVE ENGAGEMENT**: After every general concierge response, end with exactly ONE warm, relevant follow-up question to keep the conversation going. Draw the question from GINI Bali's actual service scope: accommodation/stay, tours & sightseeing, transport & pickup/drop, relaxation & wellness, massage & spa, cultural experiences, events & festivals, or service bookings. One question only — never a list of questions. **EXCEPTION — SKIP THIS RULE ENTIRELY if your PERSONA is a translator, currency converter, passport assistant, or maintenance reporter. Those tools must output ONLY their functional result and nothing else.**\n"
                "- **FORMAT DISCIPLINE** (2026-08-07): Keep replies short — a few sentences or a tight bulleted list, never one dense wall of text. For a big multi-part request, don't answer everything at once — ask one narrowing question first. Use 1-3 emojis naturally where relevant, never forced. If the guest says they don't understand or repeats a question, do not restate your previous message in different words — simplify to the single most useful fact or action. **SAME EXCEPTION as PROACTIVE ENGAGEMENT above** applies (skip for translator/currency/passport/maintenance tools)."
            )

        prompt = f"""SYSTEM:
{villa_context}
{guest_context_line}{order_context}
Current Date and Time in Bali: {current_time}

PERSONA:
{persona}

{villa_rules_context + chr(10) if villa_rules_context else ""}{"INTERNAL DB (use this first for prices/services):" + chr(10) + sheet_ctx if sheet_ctx else ""}

{"KNOWLEDGE BASE:" + chr(10) + rag_ctx if rag_ctx else ""}

RULES:
{_rules_block}

CONVERSATION HISTORY:
{history}"""
        # ── Input size guard ─────────────────────────────────────────────────
        _max_chars = settings.OPENAI_MAX_INPUT_CHARS
        if _max_chars > 0 and len(query) > _max_chars:
            logger.warning(f"[ai-budget] prompt too large ({len(query)} chars > {_max_chars}) — rejected before OpenAI call.")
            return "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581."

        # ── Daily budget check ───────────────────────────────────────────────
        try:
            await ai_budget_guard.check("chat", estimated_input_chars=len(query) + len(prompt))
        except BudgetExceeded as _be:
            logger.warning(f"[ai-budget] web chat blocked: {_be.reason}")
            await ai_budget_guard.record("chat", blocked=True, block_reason=_be.reason,
                                         villa_code=villa_code)
            return "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581."

        import asyncio as _asyncio
        for _attempt in range(2):  # 1 retry on transient failure
            try:
                _model = model or settings.OPENAI_MODEL_NAME
                comp = await client.chat.completions.create(
                    model=_model,
                    messages=[{"role": "system", "content": prompt}, {"role": "user", "content": query}],
                    temperature=0.7,
                    max_tokens=600
                )
                _resp_text = comp.choices[0].message.content or "I'm here to help! How can I assist?"
                _usage = getattr(comp, "usage", None)
                await ai_budget_guard.record(
                    "chat",
                    input_tokens=getattr(_usage, "prompt_tokens", 0),
                    output_tokens=getattr(_usage, "completion_tokens", 0),
                    model=_model,
                    villa_code=villa_code,
                )
                return _resp_text
            except BudgetExceeded:
                raise
            except Exception as e:
                logger.error(f"OpenAI call error (attempt {_attempt + 1}): {e}")
                print(f"🔴 OpenAI FAIL attempt {_attempt + 1} | model={settings.OPENAI_MODEL_NAME} | villa={villa_code} | query={query[:60]!r} | {type(e).__name__}: {e}")
                if _attempt == 0:
                    await _asyncio.sleep(2)  # brief back-off before retry
        return "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581."

    def _voice_translator_hi(self, user_id, lang):
        txt = "Halo! Saya mentor bahasa Anda. Mau belajar kata-kata keren dalam Bahasa Bali atau Indonesia hari ini? 🌴" if lang == "ID" else "Hi there! I'm your Language Mentor. Want to learn some cool Balinese or Indonesian phrases today? 🌴"
        save_message(user_id, "assistant", txt)
        return {"response": txt}

    def _passport_hi(self, user_id, lang):
        txt = (
            "🛂 Hai! Untuk mengirim paspor Anda, silakan isi formulir di atas — masukkan nama lengkap "
            "Anda, ketuk kotak unggah untuk melampirkan foto paspor, lalu tekan Kirim Paspor dengan Aman."
        ) if lang == "ID" else (
            "🛂 Hi! To submit your passport, please use the form above this chat — enter your full name, "
            "tap the upload box to attach your passport photo, then tap Securely Submit Passport."
        )
        save_message(user_id, "assistant", txt)
        return {"response": txt}

    async def _handle_booking(self, query, user_id, lang, check, villa_code="WEB_VILLA_01"):
        try:
            matched_name = check.get("matched_service") or query
            intent = ai_menu_generator.detect_service_intent(matched_name)
            if intent:
                reqs = ai_menu_generator.extract_requirements(query)
                menu = await ai_menu_generator.generate_service_menu(intent["category"], intent["subcategory"], reqs, villa_code)
                if menu and "sections" in menu:
                    title = f"Pilihan untuk {intent['subcategory']}" if lang == "ID" else f"Options for {intent['subcategory']}"
                    table = {
                        "type": "service_selection", 
                        "title": title, 
                        "message": (f"Berikut layanan {intent['subcategory']} yang tersedia untuk villa Anda:" if lang == "ID" else f"Here are the {intent['subcategory']} services available for your villa stay:"),
                        "options": menu["sections"][0]["rows"]
                    }
                    resp = f"SERVICES_DATA|{json.dumps(table)}"
                    save_message(user_id, "assistant", resp)
                    return {"response": resp}
        except Exception as e:
            logger.error(f"Handle Booking Error: {e}")
        return None

concierge_ai = ConciergeAI()
async def generate_response(query:str, user_id:str, chat_type:str="general", language:str="EN", villa_code:str="WEB_VILLA_01"):
    # ── Kill switch ─────────────────────────────────────────────────────────────
    # Check AI_ENABLED / AI_EMERGENCY_KILL_SWITCH before anything else.
    # BudgetExceeded from kill switch is caught here so the response is the same
    # standard error message regardless of the block reason.
    try:
        await ai_budget_guard.check("chat", estimated_input_chars=len(query))
    except BudgetExceeded as _be:
        logger.warning(f"[ai-budget] generate_response blocked for user={user_id}: {_be.reason}")
        await ai_budget_guard.record("chat", blocked=True, block_reason=_be.reason,
                                     villa_code=villa_code, user_id=user_id)
        return {"response": "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581."}

    # ── Test-sender cost guard ──────────────────────────────────────────────────
    # Never spend real OpenAI credits on test/simulation senders. CI and Playwright
    # runs use user_ids prefixed test_ / sim_ and hit the live production backend;
    # without this guard every run burns tokens. Real guests are unaffected.
    if user_id and str(user_id).lower().startswith(("test_", "sim_")):
        logger.info(f"[cost-guard] OpenAI skipped for test sender: {user_id}")
        return {"response": "Test mode — AI response skipped (no OpenAI call made)."}
    return await concierge_ai.process_query(query, user_id, chat_type, language, villa_code)
