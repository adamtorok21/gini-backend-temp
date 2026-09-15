import os
import httpx
import datetime
import dateutil.parser
import re
import logging
import asyncio
import json
from typing import Optional, Dict, List, Any

from app.services.whatsapp_ai_prompt import whatsapp_response
from app.services.guest_service import get_guest_context_by_phone
from app.models.guest_profile import GuestProfile
from app.services.ai_menu_generator import ai_menu_generator
from app.services.invoice_generator import generate_and_upload_invoice
from app.services.menu_services import get_service_provider_by_whatsapp, get_villa_code_by_name, get_location_specific_price, get_villa_info_by_code, get_dnp_categories, get_dnp_subcategories, get_dnp_promo
from app.services.order_summary import initiate_chat_session, active_chat_sessions, save_order_to_db, check_order_confirmation, order_sessions, update_order_confirmation, get_sender_id_by_order, get_order_by_number
from app.settings.config import settings
from app.utils.media_upload import process_whatsapp_passport, process_whatsapp_issue
from app.db.session import db, order_collection, villa_code_collection, checkin_collection, inquiry_collection, issue_collection, feedback_collection, customer_collection, booking_sessions_collection, guest_profile_collection, villa_code_sessions_collection
from pymongo import ReturnDocument
from app.models.order_summary import Order
from app.services.websocket_managerr import ConnectionManager
from app.services.website_sess import website_sessions
from app.utils.language_lesson_whatsapp_fucntions import language_starting_message, language_yes_message, language_lesson_response, language_no_message
from app.services.payment_service import create_xendit_payment_with_distribution, update_order_with_payment_info

logger = logging.getLogger(__name__)

manager = ConnectionManager()

villa_code_sessions = {}
issue_reporting_sessions = {}
feedback_sessions = {}
passport_sessions = {}
amenity_wa_sessions: Dict[str, dict] = {}  # sender_id -> {step, villa_code, selected_item, id_map, timestamp}
dnp_wa_sessions: Dict[str, dict] = {}      # sender_id -> {step, category, id_map, timestamp}
pending_media_sessions = {}   # sender_id -> {media_id, media_type, timestamp} — awaiting issue/passport choice

# Post-arrival onboarding: collect check-in date + stay duration from guest
# { sender_id: { "step": "awaiting_checkin_date"|"awaiting_duration", "villa_code": str, "checkin_date": datetime } }
onboarding_sessions: Dict[str, dict] = {}

local_order_store: Dict[str, str] = {}

decline_sessions : Dict[str, str] = {}

# Sheet-driven navigation state: tracks multi-step navigation per sender
# { sender_id: { "main_menu": str, "category": str, "id_map": {shcat_N/shsub_N: name} } }
sheet_nav_sessions: Dict[str, dict] = {}

# Follow-up button state: { sender_id: { "fup_0": {"query": str, "key": str}, ... } }
followup_sessions: Dict[str, dict] = {}

# Step-by-step booking state — now backed by MongoDB (booking_sessions_collection)
# In-memory dict kept only as a fallback reference; all reads/writes go through helpers below.
pending_booking_sessions: Dict[str, dict] = {}

# ── MongoDB booking session helpers ─────────────────────────────────────────
async def _get_booking_session(sender_id: str) -> dict:
    doc = await booking_sessions_collection.find_one({"sender_id": sender_id})
    return doc or {}

async def _save_booking_session(sender_id: str, data: dict) -> None:
    await booking_sessions_collection.update_one(
        {"sender_id": sender_id},
        {"$set": {**data, "sender_id": sender_id, "updated_at": datetime.datetime.now()}},
        upsert=True,
    )

async def _delete_booking_session(sender_id: str) -> dict:
    doc = await booking_sessions_collection.find_one_and_delete({"sender_id": sender_id})
    return doc or {}

# ── Villa-code collection session helpers (MongoDB-backed, restart-safe) ──────
async def _get_vc_session(sender_id: str):
    """Return the persisted villa-code session value, or None if absent."""
    doc = await villa_code_sessions_collection.find_one({"sender_id": sender_id})
    if not doc:
        return None
    return doc.get("value")

async def _save_vc_session(sender_id: str, value) -> None:
    """Upsert a villa-code session.  value can be a str or dict."""
    await villa_code_sessions_collection.update_one(
        {"sender_id": sender_id},
        {"$set": {"value": value, "sender_id": sender_id, "updated_at": datetime.datetime.now()}},
        upsert=True,
    )

async def _del_vc_session(sender_id: str) -> None:
    """Delete the villa-code session for sender_id (no-op if absent)."""
    await villa_code_sessions_collection.delete_one({"sender_id": sender_id})
# ─────────────────────────────────────────────────────────────────────────────


async def get_or_create_customer(sender_id: str) -> str:
    """Return existing customer_id or create a new one for this phone number."""
    try:
        customer = await customer_collection.find_one({"phone": sender_id})
        if customer:
            await customer_collection.update_one(
                {"phone": sender_id},
                {"$set": {"last_active": datetime.datetime.now()}}
            )
            return customer["customer_id"]
        counter = await db.counters.find_one_and_update(
            {"_id": "customer_number"},
            {"$inc": {"sequence_value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        customer_id = f"EB-C-{counter['sequence_value']:05d}"
        await customer_collection.insert_one({
            "customer_id": customer_id,
            "phone": sender_id,
            "name": None,
            "villa_code": None,
            "first_contact": datetime.datetime.now(),
            "last_active": datetime.datetime.now(),
        })
        logger.info(f"New customer created: {customer_id} for ...{sender_id[-4:]}")
        return customer_id
    except Exception as e:
        logger.error(f"get_or_create_customer error for {sender_id}: {e}")
        return f"EB-C-TEMP-{sender_id[-6:]}"


TIME_SLOTS = [
    '08:00 AM - 10:00 AM',
    '10:00 AM - 12:00 AM',
    '12:00 PM - 2:00 PM',
    '02:00 PM - 4:00 PM',
    '04:00 PM - 6:00 PM',
    '06:00 PM - 8:00 PM'
]

_CURRENCY_CODES = {"idr", "usd", "eur", "gbp", "sgd", "aud", "jpy", "cny", "myr", "thb", "chf", "inr", "krw", "hkd", "twd", "php", "vnd", "brl", "rub", "cad"}
_CURRENCY_WORDS = {"convert", "conversion", "exchange", "rate", "rupiah", "dollar", "euro", "pound", "yen", "yuan", "baht", "franc", "ringgit", "rupee", "won", "peso", "dong"}
_CURRENCY_AMOUNT = re.compile(r'\d[\d,.]*\s*(idr|usd|eur|gbp|sgd|aud|jpy|cny|myr|thb|chf|inr|krw|hkd|twd|php|vnd|brl|rub|cad)', re.IGNORECASE)

def _is_currency_query(text: str) -> bool:
    """Return True if the message looks like a currency conversion request."""
    if _CURRENCY_AMOUNT.search(text):
        return True
    words = set(re.findall(r'[a-z]+', text.lower()))
    return len(words & (_CURRENCY_CODES | _CURRENCY_WORDS)) >= 2

async def _fetch_live_rates_for_whatsapp() -> str:
    """Fetch ALL live USD exchange rates and return a compact string for AI context."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://open.er-api.com/v6/latest/USD")
            if resp.status_code == 200:
                rates = resp.json().get("rates", {})
                if rates:
                    parts = [f"1 USD = {v:,.4f} {k}" for k, v in sorted(rates.items())]
                    return "Live exchange rates (base USD): " + " | ".join(parts)
    except Exception as e:
        logger.warning(f"Live rate fetch failed: {e}")
    return ""

async def _enrich_currency_query(text: str) -> str:
    """Prepend live exchange rates to a currency query so the AI can do exact math."""
    live_rates = await _fetch_live_rates_for_whatsapp()
    if live_rates:
        return f"[LIVE_RATES: {live_rates}]\nUSER QUERY: {text}"
    return text

async def fetch_explore_data(api_url: str, query: str, user_id: str):
    payload = {"query": query}
    params = {"user_id": user_id}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(api_url, params=params, json=payload)
            response.raise_for_status()
            data = response.json()
            print(f"DEBUG: Response from API: {data}")
            return data.get("response")
    except Exception as e:
        print(f"❌ fetch_explore_data error: {e}")
        return None

language_lesson_sessions = {}
persistent_mode_sessions = {}

# Maps WhatsApp list row IDs → AI chat_type strings for direct generate_response calls.
# Keys must match the sanitized IDs produced by fetch_menu_data.
PERSISTENT_MODE_CHAT_TYPES = {
    "what_to_do_today":      "what-to-do",
    "things_to_do_in_bali":  "things-to-do-in-bali",
    "event_calendar":        "event-calender",
    "local_cousine_guide":   "local-cuisine",
    "local_cuisine_guide":   "local-cuisine",
    "plan_my_trip":          "plan-my-trip",
    "currency_converter":    "currency-converter",
    "voice_translator":      "voice-translator",
    "recommendations_interactive": "recommendations-interactive",
    # "recommendations" intentionally excluded — must fall through to _SUBMENU_PARENTS
    "discount__promotions":  "general",
    # Follow-up context keys (Option A/B — free text after follow-up buttons)
    "safety":                "things-to-do-in-bali",
    "medical":               "things-to-do-in-bali",
    "cuisine":               "local-cuisine",
    "events":                "event-calender",
    "activities":            "things-to-do-in-bali",
    "etiquette":             "things-to-do-in-bali",
    "followup_general":      "general",
}

# Context-specific kickoff messages sent when user first enters a mode.
# These replace the generic "Hi" so the AI immediately delivers value.
_KICKOFF_MESSAGES = {
    "what_to_do_today": (
        "The guest just opened 'What To Do Today'. DO NOT give them a list of activities yet. "
        "Instead, engage them by asking: 'What mood are you in? What are you into? Are you open for some adventurous activities, laid back experience, or something cultural?' "
        "Keep it friendly and short."
    ),
    "plan_my_trip": (
        "The guest wants to plan their Bali trip. Ask them these questions ONE AT A TIME "
        "in a friendly way: 1) When do they arrive and how many days are they staying? "
        "2) How many people? 3) What are their main interests — adventure, culture, relaxation, "
        "food, nightlife, or a mix? Start immediately with question 1."
    ),
    "event_calendar": (
        "The guest wants to know about events and happenings in Bali. DO NOT list events yet. "
        "Instead, initiate an interactive session by asking: 'How long are you going to stay in Bali, from which date to which date?'"
    ),
    "things_to_do_in_bali": (
        "The guest wants to explore Bali. Give them the top 5 must-do experiences — "
        "mix of adventure, culture, and local experiences. Use a numbered list."
    ),
    "local_cousine_guide": (
        "The guest wants to discover Bali food. DO NOT list foods yet. INITIATE the conversation by asking: "
        "'What mood are you in? What are you into? Are you open for some adventurous cuisine, laid back food, "
        "safe food, or sea food?'"
    ),
    "local_cuisine_guide": (
        "The guest wants to discover Bali food. DO NOT list foods yet. INITIATE the conversation by asking: "
        "'What mood are you in? What are you into? Are you open for some adventurous cuisine, laid back food, "
        "safe food, or sea food?'"
    ),
    # "hi" triggers the hardcoded clean translator greeting in generate_response — no OpenAI call, never fails
    "voice_translator": "hi",
}

# Keep for backward-compatibility with unit tests that import this name.
PERSISTENT_API_MAPPING = {k: {"url": "", "fetch_func": None} for k in PERSISTENT_MODE_CHAT_TYPES}


async def _whatsapp_ai_chat(sender_id: str, query: str, chat_type: str) -> str | None:
    """Call generate_response directly — no HTTP round-trip to self.

    Returns the response text, or None if already handled (e.g. booking flow sent).
    Intercepts the SERVICES_DATA| sentinel and routes it to the order flow UI.
    """
    from app.services.ai_prompt import generate_response
    try:
        if chat_type == "currency-converter":
            query = await _enrich_currency_query(query)
        # Voice-translator uses an isolated history key so the SMART FLIP RULE
        # only sees messages from the translation session, not general WhatsApp
        # service inquiries or concierge conversations.
        ai_user_id = f"{sender_id}_vt" if chat_type == "voice-translator" else sender_id
        result = await generate_response(
            query=query,
            user_id=ai_user_id,
            chat_type=chat_type,
            language="EN",
        )
        if not result:
            return None
        response_text = result.get("response", "")
        # Intercept SERVICES_DATA sentinel — render as booking flow, not raw text
        if response_text.startswith("SERVICES_DATA|"):
            try:
                import json as _json
                menu_data = _json.loads(response_text[len("SERVICES_DATA|"):])
                await send_ai_whatsapp_order_flow_message(
                    sender_id,
                    flow_token=f"book_{sender_id}",
                    menu_data=menu_data,
                )
            except Exception as parse_err:
                print(f"❌ SERVICES_DATA parse error: {parse_err}")
            return None  # already handled (or failed silently)
        return response_text
    except Exception as e:
        print(f"❌ _whatsapp_ai_chat error ({chat_type}): {e}")
        return None


def _infer_chat_type(text: str) -> str:
    """Infer the best AI chat_type from any menu item title."""
    t = text.lower()
    if any(w in t for w in ["currency", "convert", "exchange", "rate", "money", "rupiah", "usd", "idr"]):
        return "currency-converter"
    if any(w in t for w in ["plan", "trip", "itinerary", "schedule", "travel"]):
        return "plan-my-trip"
    if any(w in t for w in ["language", "translate", "lesson", "phrase", "speak", "bahasa"]):
        return "voice-translator"
    if any(w in t for w in ["cuisine", "food", "dining", "restaurant", "eat", "dish", "street food"]):
        return "local-cuisine"
    if any(w in t for w in ["event", "calendar", "festival", "happening", "concert"]):
        return "event-calender"
    if any(w in t for w in ["activity", "activities", "attraction", "experience", "adventure"]):
        return "things-to-do-in-bali"
    return "what-to-do"


# Sub-menu parents: selecting these shows a list of sub-items from the sheet.
_SUBMENU_PARENTS = {
    "Local Guide", "Bali Handbook", "Recommendations",
    "Find Dining Options", "Discover Spots",
}

# Known button URLs — used when the sheet Button column is empty.
# Sheet always takes priority; update the sheet to override any entry here.
_KNOWN_BUTTON_URLS: dict[str, str] = {
    # Safety & health
    "Safety & Health Tips":  "https://www.canva.com/design/DAGaNLT8Owc/gDSbEepIXK4OJxdOOtx92Q/view?utm_content=DAGaNLT8Owc&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h3bad98561a",
    "Read Safety Tips":      "https://www.canva.com/design/DAGaNLT8Owc/gDSbEepIXK4OJxdOOtx92Q/view?utm_content=DAGaNLT8Owc&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h3bad98561a",
    "Medical Suggestions":        "https://www.canva.com/design/DAGbfiNdbTw/rJdf8dxswAQDXZf3XtPSqw/view?utm_content=DAGbfiNdbTw&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h17b7214f43",
    "Find Medical Help":          "https://www.canva.com/design/DAGbfiNdbTw/rJdf8dxswAQDXZf3XtPSqw/view?utm_content=DAGbfiNdbTw&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h17b7214f43",
    "Medical Reccomendation":     "https://www.canva.com/design/DAGbfiNdbTw/rJdf8dxswAQDXZf3XtPSqw/view?utm_content=DAGbfiNdbTw&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h17b7214f43",
    "Medical Reccomendations":    "https://www.canva.com/design/DAGbfiNdbTw/rJdf8dxswAQDXZf3XtPSqw/view?utm_content=DAGbfiNdbTw&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h17b7214f43",
    "Medical Recommendations":    "https://www.canva.com/design/DAGbfiNdbTw/rJdf8dxswAQDXZf3XtPSqw/view?utm_content=DAGbfiNdbTw&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h17b7214f43",
    "Do's and Don't":             "https://www.canva.com/design/DAGbZYkN0V8/SRJA3kOkFPzFqRmJEjlGbQ/view?utm_content=DAGbZYkN0V8&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h1d8411966d",
    "Know the Local Rules":       "https://www.canva.com/design/DAGbZYkN0V8/SRJA3kOkFPzFqRmJEjlGbQ/view?utm_content=DAGbZYkN0V8&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h1d8411966d",
    "Do's and Don't of Bali":     "https://www.canva.com/design/DAGbZYkN0V8/SRJA3kOkFPzFqRmJEjlGbQ/view?utm_content=DAGbZYkN0V8&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h1d8411966d",
    "Do's and Don'ts of Bali":    "https://www.canva.com/design/DAGbZYkN0V8/SRJA3kOkFPzFqRmJEjlGbQ/view?utm_content=DAGbZYkN0V8&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h1d8411966d",
    "Dos and Don'ts":             "https://www.canva.com/design/DAGbZYkN0V8/SRJA3kOkFPzFqRmJEjlGbQ/view?utm_content=DAGbZYkN0V8&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h1d8411966d",
    "Dos and Don'ts of Bali":     "https://www.canva.com/design/DAGbZYkN0V8/SRJA3kOkFPzFqRmJEjlGbQ/view?utm_content=DAGbZYkN0V8&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h1d8411966d",
    "Safety and Health Tips":     "https://www.canva.com/design/DAGaNLT8Owc/gDSbEepIXK4OJxdOOtx92Q/view?utm_content=DAGaNLT8Owc&utm_campaign=designshare&utm_medium=link2&utm_source=uniquelinks&utlId=h3bad98561a",
    # Shopping & spots
    "Shop the Best Places":  "https://maps.app.goo.gl/soEShhHPXVJhM5ms6",
    "Find Your Spot":        "https://maps.app.goo.gl/YvPCmHqJTh2ZNz3HA",
    "Relax & Recharge":      "https://maps.app.goo.gl/26FqxqXMmgvdyXvRA",
    "Explore After Dark":    "https://maps.app.goo.gl/NcKZhc3vRUeqKF6M7",
    "Locate Hospital":       "https://maps.app.goo.gl/SVWEZTNwhZUjPpZR6",
}

from app.services.menu_services import (
    get_main_menu_design,
    get_sheet_menu_categories,
    get_sheet_menu_subcategories,
    get_sheet_menu_sub_subcategories,
    get_sheet_menu_endpoint,
)

# Menus that use the Menu Structure sheet for navigation (Main Menu → Category → Sub-category → Endpoint)
# Maps the WhatsApp button display name → the "Main Menu" column value in the Menu Structure sheet.
# "Local Guide" is kept as alias because the Menu Design sheet may still use the old name.
_SHEET_DRIVEN_MENUS: dict[str, str] = {
    "Bali Handbook":  "Bali Handbook",
    "Local Guide":    "Bali Handbook",
    "Recommendations": "Recommendations",
    "What To Do Today?": "What To Do Today",
}

# Menus that use interactive BUTTONS for navigation instead of list messages (≤3 items per level).
# Falls back to list automatically if a level has more than 3 items.
_BUTTON_NAV_MENUS: set = {"Recommendations"}


async def _send_nav_buttons(sender_id: str, body: str, buttons: list) -> None:
    """Send up to 3 reply buttons for sheet-driven navigation."""
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
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
                    for b in buttons[:3]
                ]
            },
        },
    }
    async with httpx.AsyncClient() as _hc:
        resp = await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)
        resp.raise_for_status()


# ── Follow-up prompt infrastructure (Option A + B) ────────────────────────────
# Contextual quick-reply buttons sent after every endpoint execution.
# 3 buttons max (WhatsApp limit); titles ≤ 20 chars.
_FOLLOWUP_CONFIGS: dict[str, list] = {
    "safety": [
        ("🏥 Find Hospital",    "Where are the nearest hospitals and clinics in Bali?"),
        ("💊 Pharmacy",         "Where can I find a pharmacy in Bali?"),
        ("🚑 Emergency",        "What are the emergency numbers and contacts in Bali?"),
    ],
    "medical": [
        ("🏥 Find Clinic",      "Where are medical clinics in Bali?"),
        ("💊 Buy Medicines",    "Where can I buy medicines or get a prescription in Bali?"),
        ("🚑 Emergency",        "What are the emergency contacts in Bali?"),
    ],
    "cuisine": [
        ("🍜 Must Try Food",    "What food must I absolutely try in Bali?"),
        ("🍽️ Best Warungs",    "What are the best warungs and local restaurants in Bali?"),
        ("🥤 Local Drinks",     "What local drinks should I try in Bali?"),
    ],
    "events": [
        ("🎪 This Week",        "__THIS_WEEK_EVENTS__"),
        ("🌺 Festivals",        "What festivals and ceremonies are coming up in Bali?"),
        ("🎵 Music & Nightlife","What music events and nightlife are in Bali?"),
    ],
    "activities": [
        ("🏄 Water Sports",     "What water sports can I do in Bali?"),
        ("🌺 Cultural Tours",   "What cultural tours and experiences are available in Bali?"),
        ("🌅 Nightlife",        "What is the nightlife scene like in Bali?"),
    ],
    "etiquette": [
        ("👗 Dress Code",       "What is the dress code and temple etiquette in Bali?"),
        ("🙏 Temple Rules",     "What are the rules when visiting temples in Bali?"),
        ("💰 Tipping",          "What is the tipping culture in Bali?"),
    ],
    "followup_general": [
        ("🌴 Explore Bali",     "What are the top things to see and do in Bali for tourists?"),
        ("💰 Book a Service",   "What services can I book through GINI Bali?"),
    ],
}


def _get_followup_key(title: str, main_menu: str = "") -> str:
    t = title.lower()
    m = main_menu.lower()
    if "safety" in t or "health" in t:
        return "safety"
    if "medical" in t or "hospital" in t or "clinic" in t or "reccom" in t:
        return "medical"
    if "cuisine" in t or "food" in t or "dining" in t or "cousine" in t or "restaurant" in t:
        return "cuisine"
    if "event" in t or "calendar" in t or "festival" in t or "ceremony" in t:
        return "events"
    if "things to do" in t or "activit" in t or "what to do" in t:
        return "activities"
    if "don't" in t or "dos" in t or "rule" in t or "etiquette" in t or "do's" in t:
        return "etiquette"
    if "recommendation" in m or "recommendation" in t:
        return "activities"  # Recommendations items default to activities context
    return "followup_general"


async def _send_followup_prompt(sender_id: str, title: str, main_menu: str = "") -> None:
    """Send contextual follow-up buttons (Option B) + open invite (Option A) after an endpoint executes."""
    key = _get_followup_key(title, main_menu)
    config = _FOLLOWUP_CONFIGS.get(key, _FOLLOWUP_CONFIGS["followup_general"])
    btn_map = {}
    btns = []
    for i, (btn_title, query) in enumerate(config):
        bid = f"fup_{i}"
        btn_map[bid] = {"query": query, "key": key}
        btns.append({"id": bid, "title": btn_title[:20]})
    followup_sessions[sender_id] = btn_map
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
            "body": {"text": "💬 Anything else I can help with?\nTap a question or just type your own!"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
                    for b in btns
                ]
            },
        },
    }
    try:
        async with httpx.AsyncClient() as _hc:
            resp = await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as _fe:
        logger.warning(f"Could not send follow-up prompt: {_fe}")
        try:
            await send_whatsapp_message(sender_id, "💬 Have a question? Just type it and I'll help!")
        except Exception:
            pass
# ──────────────────────────────────────────────────────────────────────────────


# ─── Interactive booking flow helpers ────────────────────────────────────────

def _build_form_card(service_name: str, price: str, session: dict) -> str:
    """Build a visual form card showing filled / active / pending fields."""
    step = session.get("step", "")
    name    = session.get("name")
    phone   = session.get("phone")
    date    = session.get("date")
    time_v  = session.get("time")
    persons = session.get("persons")

    price_line = f"\n💰 *Price:* {price} per person" if price else ""

    def field(icon, label, value, active_hint):
        if value:
            return f"✅ {icon} *{label}:* {value}"
        if step and label.lower().replace(" ", "_") in step or step == f"awaiting_{label.lower().replace(' ', '_')}":
            return f"📝 {icon} *{label}:* ← _{active_hint}_"
        return f"⬜ {icon} *{label}:* —"

    name_line    = f"✅ 👤 *Full Name:* {name}"    if name    else (f"📝 👤 *Full Name:* ← _Type your full name_"               if step == "awaiting_name"    else "⬜ 👤 *Full Name:* —")
    phone_line   = f"✅ 📞 *Phone:* {phone}"        if phone   else (f"📝 📞 *Phone:* ← _Include country code, e.g. +62812..._" if step == "awaiting_phone"   else "⬜ 📞 *Phone:* —")
    date_line    = f"✅ 📅 *Date:* {date}"          if date    else (f"📝 📅 *Date:* ← _Type date, e.g. 28/03/2026_"            if step == "awaiting_date"    else "⬜ 📅 *Date:* —")
    time_line    = f"✅ ⏰ *Time:* {time_v}"        if time_v  else (f"📝 ⏰ *Time:* ← _Select a slot below_"                   if step == "awaiting_time"    else "⬜ ⏰ *Time:* —")
    persons_line = f"✅ 👥 *Persons:* {persons}"    if persons else (f"📝 👥 *Persons:* ← _Select below_"                       if step == "awaiting_persons" else "⬜ 👥 *Persons:* —")

    return (
        f"📋 *Booking Form — {service_name}*{price_line}\n"
        f"──────────────────────\n"
        f"{name_line}\n"
        f"{phone_line}\n"
        f"{date_line}\n"
        f"{time_line}\n"
        f"{persons_line}\n"
        f"──────────────────────"
    )


async def _start_booking_flow(sender_id: str, service_name: str, price_display: str = "") -> None:
    """Open the BOOKING screen of the Category (Order Services) Flow directly.
    WCR-14: Sends Category Flow with flow_token = service|{name}|{price}.
    /category-flow INIT detects this prefix and returns BOOKING screen data directly,
    skipping the 3 category-selection screens.  Service name is recoverable from
    the token itself (WCR-15b) and also persisted to MongoDB (WCR-15a).
    Final fallback: web link.
    """
    from app.services.whatsapp_flows_service import send_category_flow_message

    # service| prefix tells /category-flow INIT to return BOOKING screen directly
    flow_token = f"service|{service_name}|{price_display}"

    # Non-fatal: save booking session so nfm_reply can look up service_name by flow_token
    try:
        await _save_booking_session(sender_id, {
            "flow_token": flow_token,
            "service_name": service_name,
            "price": price_display,
            "step": "flow_sent",
        })
    except Exception as _sess_err:
        logger.warning(f"[_start_booking_flow] session save failed (non-fatal): {_sess_err}")

    # Primary: Category Flow (data_exchange) with service| token → INIT jumps to BOOKING
    cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
    try:
        _price_line = f" ({price_display})" if price_display else ""
        await send_category_flow_message(
            sender_id,
            cat_flow_id,
            flow_token,
            cta="Book Now",
            header_text="Book Your Service",
            body_text=f"You selected *{service_name}*{_price_line}. Tap below to open the booking form.",
        )
        return
    except Exception as _bk_err:
        logger.error(f"[_start_booking_flow] category flow send failed: {_bk_err}")

    # Final fallback: web link
    import urllib.parse
    raw_price = re.sub(r'[^\d]', '', price_display) if price_display else "0"
    encoded_service = urllib.parse.quote(service_name)
    booking_url = f"{settings.WEB_BASE_URL}/book?service={encoded_service}&price={raw_price}&wa={sender_id}"
    price_line = f"\n💰 *Price:* {price_display} per person" if price_display else ""
    await send_whatsapp_message(
        sender_id,
        f"Great choice! ✨ You selected *{service_name}*.{price_line}\n\nTap to open your booking form:\n{booking_url}"
    )
    await send_whatsapp_interactive_link_with_text(sender_id, booking_url, "📋 Open Booking Form", f"Fill in your details to book {service_name}")


# ── Villa-code JIT gate for non-"Order Services" booking entry points ─────────
# The "Order Services" menu tap has always gated on villa code before opening any
# flow (see the "order_services" tap handlers above). Booking entry points
# reached via natural-language / AI-suggested service taps (ai_catalog_*,
# ai_service_*, service_*) called _start_booking_flow() directly with no such gate,
# so a guest with no villa code could fill in an entire booking form and only be
# rejected at the very last step (bk_confirm's resolve_customer_context check) —
# confirmed live via production BLOCKED: MISSING_VILLA_CODE errors. These two
# helpers extend the same JIT-ask pattern to those entry points, using
# booking_sessions_collection (not villa_code_sessions_collection) to remember
# which service/price to resume once the villa code is known.
async def _gate_booking_on_villa_code(sender_id: str, service_name: str, price_display: str = "") -> bool:
    """Call before _start_booking_flow() from any non-order_services entry point.
    Returns True if the guest was asked for their villa code instead (caller must
    return immediately). Returns False if the guest already has a villa code and
    the caller should proceed to _start_booking_flow() as normal."""
    if await get_user_villa_code(sender_id):
        return False
    await _save_booking_session(sender_id, {
        "step": "awaiting_villa_before_booking",
        "service_name": service_name,
        "price": price_display,
    })
    await _save_vc_session(sender_id, "start_booking")
    await send_whatsapp_message(
        sender_id,
        "To show you the right services and prices, we need to know your villa.\n\n"
        "Do you have your *villa code*? It's a short code like *V1* or *V2* — "
        "you'll find it on your welcome card or villa QR sticker.\n\n"
        "• *Type your villa code* — e.g. V1\n"
        "• Reply *no* if you don't have it and we'll help you find your villa"
    )
    return True


async def _try_resume_pending_booking(sender_id: str) -> bool:
    """Call from the villa-code-resolved resume points once a villa code has just
    been saved. If a booking was paused by _gate_booking_on_villa_code(), resumes
    that specific service's booking form and returns True (caller must return
    immediately). Returns False if there was no pending booking (caller should
    fall through to its normal order_services resume behaviour)."""
    pending = await _get_booking_session(sender_id)
    if not pending or pending.get("step") != "awaiting_villa_before_booking":
        return False
    await _delete_booking_session(sender_id)
    try:
        await _start_booking_flow(sender_id, pending.get("service_name", ""), pending.get("price", ""))
    except Exception as _resume_err:
        logger.error(f"[_try_resume_pending_booking] resume failed for {sender_id}: {_resume_err}")
        await send_whatsapp_message(
            sender_id,
            f"✅ Villa saved! Type *Order Services* to book *{pending.get('service_name') or 'your service'}* again."
        )
    return True


async def _send_time_buttons(sender_id: str) -> None:
    """Show updated form card + time slot buttons."""
    session = await _get_booking_session(sender_id)
    card = _build_form_card(session.get("service_name", ""), session.get("price", ""), session)
    headers = {"Authorization": f"Bearer {settings.access_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": card},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "bk_time_1", "title": "12:00 – 14:00"}},
                    {"type": "reply", "reply": {"id": "bk_time_2", "title": "14:00 – 16:00"}},
                    {"type": "reply", "reply": {"id": "bk_time_3", "title": "16:00 – 18:00"}},
                ],
            },
        },
    }
    async with httpx.AsyncClient() as _hc:
        await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)


async def _send_persons_list(sender_id: str) -> None:
    """Show updated form card + number of persons as a list message (4 options)."""
    session = await _get_booking_session(sender_id)
    card = _build_form_card(session.get("service_name", ""), session.get("price", ""), session)
    headers = {"Authorization": f"Bearer {settings.access_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": card},
            "action": {
                "button": "Select Persons",
                "sections": [{
                    "title": "Number of Guests",
                    "rows": [
                        {"id": "bk_persons_1", "title": "1 Person"},
                        {"id": "bk_persons_2", "title": "2 People"},
                        {"id": "bk_persons_3", "title": "3 People"},
                        {"id": "bk_persons_4", "title": "4 People"},
                    ],
                }],
            },
        },
    }
    async with httpx.AsyncClient() as _hc:
        await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)


async def _send_booking_summary(sender_id: str) -> None:
    """Show filled form card + Confirm / Cancel buttons."""
    session = await _get_booking_session(sender_id)
    service  = session.get("service_name", "")
    price    = session.get("price", "")
    name     = session.get("name", "")
    phone    = session.get("phone", "")
    date_str = session.get("date", "")
    time_str = session.get("time", "")
    persons  = session.get("persons", "1")

    try:
        raw = int(re.sub(r"[^\d]", "", price) or "0")
        num = int(persons) if str(persons).isdigit() else 1
        total = raw * num
        price_total = f"\n💰 *Total:* IDR {total:,}" if raw else (f"\n💰 *Price:* {price}" if price else "")
    except Exception:
        price_total = f"\n💰 *Price:* {price}" if price else ""

    summary = (
        f"📋 *Booking Summary*\n"
        f"──────────────────────\n"
        f"✅ 🧖 *Service:* {service}\n"
        f"✅ 👤 *Name:* {name}\n"
        f"✅ 📞 *Phone:* {phone}\n"
        f"✅ 📅 *Date:* {date_str}\n"
        f"✅ ⏰ *Time:* {time_str}{price_total}\n"
        f"──────────────────────\n"
        f"Please confirm your booking."
    )
    headers = {"Authorization": f"Bearer {settings.access_token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": summary},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "bk_confirm", "title": "✅ Confirm"}},
                    {"type": "reply", "reply": {"id": "bk_cancel",  "title": "❌ Cancel"}},
                ],
            },
        },
    }
    async with httpx.AsyncClient() as _hc:
        await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)


def _endpoint_to_chat_type(endpoint: str, fallback_title: str = "") -> str:
    """Derive an AI chat_type from a Menu Structure endpoint value."""
    ep = endpoint.lower()
    if "currency" in ep or "convert" in ep:
        return "currency-converter"
    if "plan" in ep or "trip" in ep or "itinerary" in ep:
        return "plan-my-trip"
    if "cuisine" in ep or "food" in ep or "dining" in ep:
        return "local-cuisine"
    if "event" in ep or "calendar" in ep or "festival" in ep:
        return "event-calender"
    if "activity" in ep or "activities" in ep or "things to do" in ep:
        return "things-to-do-in-bali"
    return _infer_chat_type(fallback_title)


# ── WhatsApp Amenity Request — item list and confirm helpers ──────────────────

_AMENITY_ITEMS_WA = [
    ("Toilet Paper",                    "wa_ami_0"),
    ("Tissues",                         "wa_ami_1"),
    ("Shampoo",                         "wa_ami_2"),
    ("Conditioner",                     "wa_ami_3"),
    ("Body Wash",                       "wa_ami_4"),
    ("Hand Soap",                       "wa_ami_5"),
    ("Towels",                          "wa_ami_6"),
    ("Pool Towels",                     "wa_ami_7"),
    ("Bed Linen",                       "wa_ami_8"),
    ("Pillows",                         "wa_ami_9"),
    ("Blankets",                        "wa_ami_10"),
    ("Slippers",                        "wa_ami_11"),
    ("Drinking Water",                  "wa_ami_12"),
    ("Coffee & Tea Refill",             "wa_ami_13"),
    ("Sugar / Sweetener",               "wa_ami_14"),
    ("Trash Bags",                      "wa_ami_15"),
    ("Cleaning Service",                "wa_ami_16"),
    ("Extra Bed Setup",                 "wa_ami_17"),
]
_AMENITY_ID_TO_ITEM: dict[str, str] = {aid: name for name, aid in _AMENITY_ITEMS_WA}


async def _send_amenity_items_list(sender_id: str, page: int = 1) -> None:
    """Send paged amenity list. Total rows per page must not exceed 10 (WhatsApp limit).

    Page 1: items 0-7 + "View More" (9 rows, section 1) | Report Issue (1 row, section 2) = 10 total
    Page 2: items 8-17 (10 rows, section 1 only) = 10 total
    """
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }

    issue_section = {
        "title": "🔧 Report Issue",
        "rows": [
            {"id": "wa_issue_from_amenity", "title": "Report Maintenance Issue", "description": "Describe a problem at your villa"},
        ],
    }

    if page == 1:
        # 8 items + "View More" = 9 rows in section 1; + 1 Report Issue = 10 total (WhatsApp max)
        amenity_rows = [
            {"id": aid, "title": name[:24], "description": "Tap to select"}
            for name, aid in _AMENITY_ITEMS_WA[:8]
        ]
        amenity_rows.append({"id": "wa_ami_more", "title": "View More Amenities...", "description": "Bed Linen, Pillows, Blankets & more"})
        sections = [
            {"title": "🛎️ Amenities", "rows": amenity_rows},
            issue_section,
        ]
    else:
        # Items 8-17 = 10 rows; no Report Issue section to stay within 10-row limit
        amenity_rows = [
            {"id": aid, "title": name[:24], "description": "Tap to select"}
            for name, aid in _AMENITY_ITEMS_WA[8:]
        ]
        sections = [{"title": "🛎️ Amenities", "rows": amenity_rows}]

    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "🛎️ Amenities & Issues"},
            "body": {"text": "Select an amenity item — villa staff will bring it to you.\n\nOr report a maintenance issue below."},
            "footer": {"text": "Type CANCEL to exit"},
            "action": {
                "button": "View Options",
                "sections": sections,
            },
        },
    }
    try:
        async with httpx.AsyncClient() as _hc:
            resp = await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as _e:
        logger.error(f"_send_amenity_items_list page={page} error: {_e}")


async def _send_amenity_confirm_buttons(sender_id: str, item: str) -> None:
    """Send 2 quick-reply buttons to confirm or change an amenity item selection."""
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
            "body": {"text": f"You selected: *{item}*\n\nShall we request this for your villa?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "wa_ami_confirm", "title": "Yes, Request It"}},
                    {"type": "reply", "reply": {"id": "wa_ami_change",  "title": "Change Item"}},
                ],
            },
        },
    }
    try:
        async with httpx.AsyncClient() as _hc:
            resp = await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as _e:
        logger.error(f"_send_amenity_confirm_buttons error: {_e}")


# ── Discounts & Promotions (DNP) helpers ────────────────────────────────────

async def _send_dnp_categories(sender_id: str) -> None:
    """Send interactive list of DNP categories to the guest."""
    try:
        categories = await get_dnp_categories()
    except Exception as _e:
        logger.error(f"_send_dnp_categories error: {_e}")
        await send_whatsapp_message(sender_id, "Sorry, couldn't load promotions right now. Please try again later.")
        return

    if not categories:
        await send_whatsapp_message(sender_id, "No active promotions available right now. Check back soon! 🎁")
        return

    id_map = {}
    rows = []
    for i, cat in enumerate(categories[:10]):
        cid = f"dnp_cat_{i}"
        id_map[cid] = cat
        rows.append({"id": cid, "title": cat[:24], "description": "View deals & promotions"})

    dnp_wa_sessions[sender_id] = {
        "step": "categories",
        "id_map": id_map,
        "timestamp": datetime.datetime.now(),
    }

    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "🎁 Discounts & Promotions"},
            "body": {"text": "Exclusive deals for GINI Bali guests.\n\nSelect a category to view available offers:"},
            "footer": {"text": "Type CANCEL to exit"},
            "action": {
                "button": "View Categories",
                "sections": [{"title": "Categories", "rows": rows}],
            },
        },
    }
    try:
        async with httpx.AsyncClient() as _hc:
            resp = await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as _e:
        logger.error(f"_send_dnp_categories send error: {_e}")


async def _send_dnp_subcategories(sender_id: str, category: str) -> None:
    """Send 2-section list: Referral (left) | General (right) for a DNP category."""
    try:
        promos = await get_dnp_subcategories(category)
    except Exception as _e:
        logger.error(f"_send_dnp_subcategories error: {_e}")
        await send_whatsapp_message(sender_id, "Sorry, couldn't load promotions for this category. Please try again.")
        return

    if not promos:
        await send_whatsapp_message(sender_id, f"No active promotions in *{category}* right now. 🎁\n\nType CANCEL to return to the main menu.")
        return

    referral = [p for p in promos if (p.get("promo_type") or "").lower() == "referral"]
    general  = [p for p in promos if (p.get("promo_type") or "").lower() != "referral"]

    ref_rows = []
    for p in referral:
        pid = (p.get("id") or "").strip()
        if not pid:
            continue
        title = (p.get("title") or p.get("name") or pid)[:24]
        desc  = (p.get("description") or "Referral deal")[:69]
        ref_rows.append({"id": f"dnp_sub_{pid.lower()}", "title": title, "description": desc})

    gen_rows = []
    for p in general:
        pid = (p.get("id") or "").strip()
        if not pid:
            continue
        title = (p.get("title") or p.get("name") or pid)[:24]
        desc  = (p.get("description") or "Special offer")[:69]
        gen_rows.append({"id": f"dnp_sub_{pid.lower()}", "title": title, "description": desc})

    # Keep within WhatsApp's 10-row total limit across all sections
    if len(ref_rows) + len(gen_rows) > 10:
        ref_limit = max(1, min(len(ref_rows), 5))
        ref_rows  = ref_rows[:ref_limit]
        gen_rows  = gen_rows[:10 - ref_limit]

    sections = []
    if ref_rows:
        sections.append({"title": "🎫 Referral", "rows": ref_rows})
    if gen_rows:
        sections.append({"title": "🎁 General", "rows": gen_rows})

    if not sections:
        await send_whatsapp_message(sender_id, f"No promotions available in *{category}* right now. Check back soon!")
        return

    dnp_wa_sessions[sender_id] = {
        "step": "promos",
        "category": category,
        "timestamp": datetime.datetime.now(),
    }

    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": f"🎁 {category[:20]}"},
            "body": {"text": f"Choose a promotion from *{category}*:"},
            "footer": {"text": "Type CANCEL to exit"},
            "action": {
                "button": "View Deals",
                "sections": sections,
            },
        },
    }
    try:
        async with httpx.AsyncClient() as _hc:
            resp = await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as _e:
        logger.error(f"_send_dnp_subcategories send error: {_e}")


async def _send_dnp_promo_detail(sender_id: str, promo_id: str) -> None:
    """Send formatted text message with full promo details (no internal fields)."""
    try:
        promo = await get_dnp_promo(promo_id)
    except Exception as _e:
        logger.error(f"_send_dnp_promo_detail error: {_e}")
        await send_whatsapp_message(sender_id, "Sorry, couldn't load promotion details. Please try again.")
        return

    if not promo:
        await send_whatsapp_message(sender_id, "This promotion is no longer available. 🎁\n\nType CANCEL to return to the main menu.")
        return

    lines = []
    title = promo.get("title") or promo.get("subcategory") or promo.get("id") or "Promotion"
    lines.append(f"🎁 *{title}*")

    subcat = promo.get("subcategory")
    if subcat and subcat != title:
        lines.append(f"📍 _{subcat}_")

    lines.append("")

    description = promo.get("description")
    if description:
        lines.append(description)
        lines.append("")

    endpoint = promo.get("endpoint")
    if endpoint:
        lines.append(endpoint)
        lines.append("")

    referral_code = promo.get("referral_code")
    if referral_code:
        lines.append(f"🎫 *Promo Code:* `{referral_code}`")

    redemption = promo.get("redemption_method")
    if redemption:
        lines.append(f"💳 *Redemption:* {redemption}")

    start_date = promo.get("start_date")
    end_date   = promo.get("end_date")
    if start_date or end_date:
        lines.append("")
        if start_date and end_date:
            lines.append(f"📅 Valid: {start_date} – {end_date}")
        elif end_date:
            lines.append(f"📅 Valid until: {end_date}")
        else:
            lines.append(f"📅 Valid from: {start_date}")

    lines.append("")
    lines.append("_Type CANCEL to return to the main menu._")

    await send_whatsapp_message(sender_id, "\n".join(lines))
    dnp_wa_sessions.pop(sender_id, None)


async def _send_report_or_amenity_choice(sender_id: str) -> None:
    """Send quick-reply buttons letting the guest choose between Report Issue and Request Amenities."""
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
            "body": {"text": "What would you like to do?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "wa_report_issue",      "title": "🔧 Report Issue"}},
                    {"type": "reply", "reply": {"id": "wa_request_amenities", "title": "🛎️ Request Amenities"}},
                ],
            },
        },
    }
    try:
        async with httpx.AsyncClient() as _hc:
            resp = await _hc.post(settings.whatsapp_api_url, json=payload, headers=headers)
            resp.raise_for_status()
    except Exception as _e:
        logger.error(f"_send_report_or_amenity_choice error: {_e}")

# ─────────────────────────────────────────────────────────────────────────────


async def _execute_sheet_endpoint(sender_id: str, endpoint: str, title: str, main_menu: str = "") -> None:
    """Execute the endpoint value from a Menu Structure sheet row."""
    # Language lesson intercept — always trigger the structured lesson flow
    _title_lower = title.lower()
    if "voice translator" in _title_lower:
        # Sheet menu item titled "Voice Translator" → start VT persistent mode
        persistent_mode_sessions[sender_id] = "voice_translator"
        data = await _whatsapp_ai_chat(sender_id, "hi", "voice-translator")
        if data:
            await send_whatsapp_message(sender_id, data)
        return
    if "language lesson" in _title_lower or "local language" in _title_lower:
        from app.utils.language_lesson_whatsapp_fucntions import language_starting_message
        language_lesson_sessions[sender_id] = {"mode": "structured", "word_index": 0, "timestamp": datetime.datetime.now()}
        await language_starting_message(sender_id)
        return
    fup_key = _get_followup_key(title, main_menu)

    def _url_button_label(url: str, item_title: str) -> tuple:
        """Return (button_text, body_text) appropriate for the URL type."""
        _u = url.lower()
        if "maps.app.goo.gl" in _u or "google.com/maps" in _u or "maps.google.com" in _u or "goo.gl/maps" in _u:
            return "View on Maps", f"📍 Here's the location for *{item_title}*. Tap to open in Google Maps."
        if "canva.com" in _u:
            return "Open Guide", f"📖 Tap below to open the *{item_title}* guide."
        if "instagram.com" in _u:
            return "View on Instagram", f"📸 Tap to view *{item_title}* on Instagram."
        return "Open Link", f"Tap below to explore *{item_title}*."

    # Direct URL → send as contextual link button, then follow-up prompt
    if endpoint.startswith("http"):
        btn_label, body = _url_button_label(endpoint, title)
        await send_whatsapp_interactive_link_with_text(sender_id, endpoint, button_text=btn_label, body_text=body)
        if main_menu.lower() == "recommendations":
            persistent_mode_sessions[sender_id] = "recommendations_interactive"
            await send_whatsapp_message(sender_id, "How can I help you further?")
            return
        persistent_mode_sessions[sender_id] = fup_key
        await _send_followup_prompt(sender_id, title, main_menu)
        return
    # Known URL fallback (for non-AI items whose sheet URL didn't load)
    known_url = _KNOWN_BUTTON_URLS.get(title) or _KNOWN_BUTTON_URLS.get(endpoint)
    if known_url:
        btn_label, body = _url_button_label(known_url, title)
        await send_whatsapp_interactive_link_with_text(sender_id, known_url, button_text=btn_label, body_text=body)
        if main_menu.lower() == "recommendations":
            persistent_mode_sessions[sender_id] = "recommendations_interactive"
            await send_whatsapp_message(sender_id, "How can I help you further?")
            return
        persistent_mode_sessions[sender_id] = fup_key
        await _send_followup_prompt(sender_id, title, main_menu)
        return
    # AI endpoint — extract specific topic from "Hybrid AI Result – <topic>"
    topic = title
    ep_lower = endpoint.lower()
    if " - " in endpoint and (ep_lower.startswith("hybrid ai") or ep_lower.startswith("ai automated")):
        topic = endpoint.split(" - ", 1)[1].strip()
    chat_type = _endpoint_to_chat_type(endpoint, title)
    mode_key = re.sub(r'[^\w]', '', title.lower().replace(" ", "_"))
    persistent_mode_sessions[sender_id] = mode_key
    # Build a comprehensive kickoff that requests full, detailed content
    if main_menu.lower() == "what to do today":
        kickoff = (
            f"The guest tapped '{title}' for What To Do Today. "
            f"DO NOT give them a massive list of activities yet. "
            f"Instead, confirm their selection of {title} and engage them by asking: "
            "'What specific area or mood are you going for right now? Do you want an adventurous experience, something laid back, or cultural? Where are you currently located?'"
        )
    else:
        kickoff = (
            f"The guest tapped '{title}' in the Bali Handbook. "
            f"Provide a COMPLETE and COMPREHENSIVE guide about '{topic}' in Bali. "
            "Cover ALL relevant sections, categories, tips, recommendations and details — "
            "include everything a tourist would want to know. "
            "Do NOT give just a brief summary or top 5 list. "
            "Use clear formatting with numbered sections and sub-points where appropriate."
        )
    data = await _whatsapp_ai_chat(sender_id, kickoff, chat_type)
    if data:
        await send_whatsapp_message(sender_id, data)
        if main_menu.lower() == "recommendations":
            persistent_mode_sessions[sender_id] = "recommendations_interactive"
            await send_whatsapp_message(sender_id, "How can I help you further?")
            return
        persistent_mode_sessions[sender_id] = fup_key
        await _send_followup_prompt(sender_id, title, main_menu)

async def fetch_menu_data(api_url: str, menu_type: str) -> list:
    try:
        data = await get_main_menu_design()
        if data is None or data.empty or "Menu Location" not in data.columns:
            return []
        filtered_data = data[data["Menu Location"] == menu_type]
        if filtered_data.empty:
            return []
        result = []
        for _, row in filtered_data.iterrows():
            result.append({
                "id": re.sub(r'[^\w]', '', row["Title"].lower().replace(" ", "_")),
                "title": row["Title"],
                "picture": row["Picture"],
                "description": row["Description"],
                "button": row["Button"]
            })
        # Combine Report Maintenance + Request For Amenities into one menu item.
        # Remove them individually (whether from sheets or prior injection) and
        # inject a single "Report Issue or Amenities" row. Keeps total ≤ 10.
        if menu_type == "Main Menu":
            result = [
                item for item in result
                if item.get("id") not in ("report_maintenance", "request_for_amenities")
            ]
            if "report_issue_or_amenities" not in {item["id"] for item in result} and len(result) < 10:
                result.append({
                    "id": "report_issue_or_amenities",
                    "title": "Report Issue/Amenities",
                    "picture": "",
                    "description": "Report a problem or request items",
                    "button": "",
                })
        return result
    except Exception as e:
        print(f" Error fetching menu data locally: {e}")
        return []

### Fetch Submenu Data
async def fetch_submenu_data(api_url: str):
    from app.services.menu_services import get_sub_menu
    try:
        # api_url looks like /menu/sub/{main_menu}
        main_menu = api_url.split("/")[-1]
        return await get_sub_menu(main_menu)
    except Exception as e:
        print(f" Error fetching submenu data locally: {e}")
        return []

async def fetch_service_items(api_url: str, subcategory: str) -> list:
    from app.services.menu_services import get_service_items
    try:
        return await get_service_items(subcategory)
    except Exception as e:
        print(f"Error fetching service items locally: {e}")
        return []
    
async def fetch_menu_design(main_menu: str, villa_code: str = "") -> dict:
    from app.services.menu_services import get_order_service_sub_menu, get_restaurants_menu, get_sub_menu
    try:
        if main_menu == "Order Services":
            return await get_order_service_sub_menu(main_menu, villa_code=villa_code or None)
        elif main_menu in ["Restaurants", "For the 'gram"]:
            return await get_restaurants_menu(main_menu)
        else:
            return await get_sub_menu(main_menu)
    except Exception as e:
        print(f"Error fetching service items locally: {e}")
        return {}
    

async def fetch_whatsapp_numbers(serviceitem: str, location_zone: str = None):
    from app.services.menu_services import get_service_overview, get_service_providers
    try:
        design_df = await get_service_overview()
        providers_df = await get_service_providers()
        
        # Robust name matching - normalize both sides
        service_norm = re.sub(r'[^a-zA-Z0-9]', '', serviceitem.lower())
        design_df['norm_item'] = design_df["Service Item"].apply(lambda x: re.sub(r'[^a-zA-Z0-9]', '', str(x).lower()))
        
        matching_items = design_df[design_df["norm_item"] == service_norm]
        if matching_items.empty:
            # Fallback for partial matches
            matching_items = design_df[design_df["norm_item"].str.contains(service_norm, na=False)]
            
        if matching_items.empty:
            logger.warning(f"⚠️ No service items found for '{serviceitem}' to notify providers.")
            return []

        # Parse provider IDs with more robustness
        all_ids = []
        for ids_str in matching_items["Service Providers"].fillna("").astype(str):
            # Split by comma or semicolon and strip spaces
            ids = [i.strip() for i in re.split(r'[,;]', ids_str) if i.strip()]
            all_ids.extend(ids)
            
        unique_provider_ids = list(set(all_ids))
        logger.info(f"🔍 Found provider IDs {unique_provider_ids} for service '{serviceitem}'")
        
        # Match providers by ID (Number column)
        matching_providers = providers_df[providers_df["Number"].astype(str).str.strip().isin(unique_provider_ids)]
        
        # --- NEW: Location-Aware Filtering ---
        if location_zone and not matching_providers.empty:
            loc_lower = location_zone.strip().lower()
            
            # Normalization mapping for synonymous zones
            CLUSTER_MAP = {
                "berawa": ["canggu", "berawa"],
                "canggu": ["canggu", "berawa"],
                "petitenget": ["seminyak", "petitenget", "kerobokan"],
                "seminyak": ["seminyak", "petitenget", "kerobokan"],
                "kerobokan": ["seminyak", "petitenget", "kerobokan"],
                "pecatu": ["uluwatu", "pecatu", "ungasan"],
                "uluwatu": ["uluwatu", "pecatu", "ungasan"],
                "ungasan": ["uluwatu", "pecatu", "ungasan"]
            }
            target_zones = CLUSTER_MAP.get(loc_lower, [loc_lower])
            
            def loc_match(row):
                avail = str(row.get("Available Locations", "")).lower()
                zones = [z.strip() for z in re.split(r'[,;]', avail) if z.strip()]
                if not zones: return True # Global fallback
                return any(tz in zones for tz in target_zones)
                
            filtered_providers = matching_providers[matching_providers.apply(loc_match, axis=1)]
            
            if not filtered_providers.empty:
                logger.info(f"📍 Filtered Providers for '{location_zone}': {filtered_providers['Number'].tolist()}")
                matching_providers = filtered_providers
            else:
                logger.warning(f"⚠️ No providers specifically for '{location_zone}'. Falling back to all providers for '{serviceitem}'.")
        
        if matching_providers.empty:
            logger.warning(f"⚠️ No providers in sheet match IDs {unique_provider_ids}")
            return []
            
        # Clean the WhatsApp numbers - MUST be digits only FOR WHATSAPP API
        #
        # WA-MULTI-SP-CELL-SPLIT-01 (2026-09-06, live report — Prakash, order
        # EB9289): a single "WhatsApp" cell can hold MORE THAN ONE SP number
        # (e.g. two providers sharing the same service row, separated by a
        # comma or newline). This loop used to apply re.sub(r'[^\d]', '', ...)
        # directly to the raw cell value with no split first — the digit-strip
        # regex removes the separator (comma/newline) right along with the
        # '+', silently MERGING two real numbers into one unusable garbled
        # digit string (e.g. "+919840705435\n+6281999281660" ->
        # "9198407054356281999281660"), which Meta's API then rejects
        # outright. Net effect: notify_service_providers() got zero numbers
        # back, no SP was ever notified, and the guest saw the generic
        # "Arranging a Service Provider" fallback message forever. Splitting
        # first (same pattern the provider-ID column already uses seven
        # lines above) lets each number be normalized independently.
        final_numbers = []
        for raw_cell in matching_providers["WhatsApp"].tolist():
            for num in re.split(r'[,;\n]', str(raw_cell)):
                clean_num = re.sub(r'[^\d]', '', num)
                if not clean_num:
                    continue

                # Indonesian Number Normalization: Convert 08xxx to 628xxx
                if clean_num.startswith('0'):
                    clean_num = '62' + clean_num[1:]
                # If it starts with 8, it's likely missing the prefix entirely
                elif clean_num.startswith('8'):
                    clean_num = '62' + clean_num

                if clean_num:
                    final_numbers.append(clean_num)
                
        logger.info(f"✅ Normalized WhatsApp numbers: {final_numbers}")
        return final_numbers
    except Exception as e:
        logger.error(f"Error fetching whatsapp numbers for provider notification: {e}")
        return []
    


async def send_typing_indicator(phone_number: str, message_id: str):
    """Send typing indicator to show bot is processing"""
    
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {
            "type": "text"
        }
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()
            print(f"✅ Typing indicator sent to {phone_number}")
    except Exception as e:
        print(f"❌ Failed to send typing indicator: {e}")





async def send_whatsapp_order_to_SP(recipient_number: str, order_summary: dict):
    """Send SP booking notification via approved template (bypasses 24-hr window)."""
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        orderid = order_summary["order_number"]
        
        # ── Test/Demo Order Exclusion ──
        sender = str(order_summary.get("sender_id", ""))
        is_test = order_summary.get("is_test", False)
        
        if is_test or sender.startswith(("test_", "sim_")):
            logger.info(f"Skipping real SP notification for test/demo order {orderid}.")
            return {"status": "skipped", "reason": "test_order"}
        # ───────────────────────────────

        # ── Idempotency Check ──
        from app.db.session import order_collection
        updated_doc = await order_collection.find_one_and_update(
            {
                "order_number": orderid,
                "notified_sps": {"$ne": recipient_number}
            },
            {
                "$addToSet": {"notified_sps": recipient_number}
            },
            return_document=True
        )
        
        if not updated_doc:
            logger.info(f"Idempotency skip: SP {recipient_number} was already notified for order {orderid}.")
            return {"status": "skipped", "reason": "already_notified"}
        # ────────────────────────

        # Resolve customer display name
        order_date = order_summary.get("booking_date") or order_summary.get("date")
        if order_date and hasattr(order_date, "strftime"):
            order_date = order_date.strftime("%Y-%m-%d")
        elif not isinstance(order_date, str) or not order_date:
            order_date = "N/A"

        # Customer contact: prefer phone_number, then sender_id if it's a real number
        _raw_sender = order_summary.get("sender_id", "")
        customer_display = (
            order_summary.get("customer_name")
            or order_summary.get("phone_number")
            or (_raw_sender if str(_raw_sender).isdigit() else None)
            or order_summary.get("customer_id")
            or "N/A"
        )

        # Resolve villa name (col B) and location (col G) for template {{8}}
        _sp_villa_code = order_summary.get("villa_code", "")
        _sp_location = order_summary.get("location_zone") or "N/A"
        # Don't use V-codes as location labels
        if _sp_location.startswith("V") and len(_sp_location) <= 4:
            _sp_location = "N/A"
        try:
            from app.services.menu_services import get_villa_info_by_code
            _sp_vinfo = await get_villa_info_by_code(_sp_villa_code) if _sp_villa_code else None
            if _sp_vinfo:
                _sp_villa_name = _sp_vinfo.get("name") or "N/A"
                _sp_location = _sp_vinfo.get("location") or _sp_location
                _sp_location = f"{_sp_villa_name}, {_sp_location}"
        except Exception:
            pass

        # Template variables must match sp_booking_notification body exactly:
        # Persons removed 2026-07-07 (Clay/user) — matches the updated
        # sp_booking_notification template (7 vars, no persons line).
        # {{1}}=customer  {{2}}=order_id  {{3}}=service  {{4}}=date
        # {{5}}=time  {{6}}=price  {{7}}=location
        template_vars = [
            str(customer_display),
            str(orderid),
            str(order_summary.get("service_name", "N/A")),
            str(order_date),
            str(order_summary.get("time", "N/A")),
            str(order_summary.get("price", "N/A")),
            str(_sp_location),
        ]

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_number,
            "type": "template",
            "template": {
                "name": "sp_booking_notification",
                "language": {"code": "en"},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": v} for v in template_vars
                        ],
                    },
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "0",
                        "parameters": [{"type": "payload", "payload": orderid}],
                    },
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": "1",
                        "parameters": [{"type": "payload", "payload": f"decline_{orderid}"}],
                    },
                ],
            },
        }

        from app.utils.notification_logger import log_notification_attempt

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            if not response.is_success:
                meta_code = None
                try:
                    meta_code = response.json().get("error", {}).get("code")
                    if meta_code is not None:
                        meta_code = int(meta_code)
                except Exception:
                    pass
                err_msg = f"HTTP {response.status_code} — {response.text[:500]}"
                logger.error(f"send_whatsapp_order_to_SP failed for {recipient_number}: {err_msg}")
                await log_notification_attempt(
                    recipient=recipient_number,
                    notification_type="sp_new_service_request",
                    channel="template",
                    success=False,
                    order_number=orderid,
                    template_name="sp_booking_notification",
                    meta_error_code=meta_code,
                    error_detail=err_msg[:200],
                )
                raise RuntimeError(err_msg)
            logger.info(f"✅ SP template notification sent to {recipient_number} for order {orderid}")
            await log_notification_attempt(
                recipient=recipient_number,
                notification_type="sp_new_service_request",
                channel="template",
                success=True,
                order_number=orderid,
                template_name="sp_booking_notification",
            )
            return response.json()

    except Exception as e:
        logger.error(f"send_whatsapp_order_to_SP exception for {recipient_number}: {e}")
        # Release the idempotency claim taken above so a genuine send failure remains
        # retryable — without this, a failed send still leaves recipient_number in
        # notified_sps forever, and every future retry silently no-ops as
        # "already_notified" even though nothing was ever actually delivered.
        try:
            _rollback_orderid = order_summary.get("order_number")
            if _rollback_orderid:
                from app.db.session import order_collection as _oc_rb
                await _oc_rb.update_one(
                    {"order_number": _rollback_orderid},
                    {"$pull": {"notified_sps": recipient_number}}
                )
        except Exception as _rollback_err:
            logger.error(f"Failed to roll back notified_sps claim for {recipient_number}: {_rollback_err}")
        raise



async def send_interactive_message(recipient_id, payment_result):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }

        payment_message = f"""Thank you for choosing GINI Bali. Please confirm your order by completing the payment through the secure link below. Once payment is confirmed, we’ll take care of the rest — just sit back, relax, and your service will come to you as scheduled."""

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "header": {
                    "type": "text",
                    "text": "🌴 Your Order Awaits!"
                },
                "body": {
                    "text": payment_message
                },
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": "Pay Now",
                        "url": payment_result['payment_url']
                    }
                }
            }
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()

    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e}")
        return None

    return response.json()




async def send_confirmation_order_to_SP(recipient_number: str, order_number: str):
    """Send the SP a confirmation prompt with guest contact + villa details.
    Per Adam's request: SP must receive the guest's user ID so they can contact them."""
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }

        # Look up order to attach guest identity to the confirmation message
        order_doc = await order_collection.find_one({"order_number": order_number})
        guest_id = "N/A"
        villa_info_line = ""
        if order_doc:
            guest_id = order_doc.get("sender_id", "N/A")
            villa_code_val = order_doc.get("villa_code", "")
            if villa_code_val:
                _sp_villa_info = await get_villa_info_by_code(villa_code_val)
                _villa_display = (_sp_villa_info or {}).get("name") or villa_code_val
                villa_info_line = f"\n🏡 *Villa:* {_villa_display}"

        body_text = (
            f"✅ *Confirm Acceptance — Order {order_number}*\n\n"
            f"Are you sure you want to accept this request? "
            f"The guest will be notified immediately.\n"
            f"_Apakah Anda yakin ingin menerima permintaan ini? Tamu akan segera diberitahu._\n\n"
            f"👤 *Guest Contact:* {guest_id}"
            f"{villa_info_line}"
        )

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_number,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"yes_order_{order_number}",
                                "title": "Yes"
                            }
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"no_order_{order_number}",
                                "title": "No, Go Back"
                            }
                        }
                    ]
                }
            }
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()

    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e}")
        return None

    return response.json()


async def send_whatsapp_interactive_link_with_text(recipient_number: str, link: str, button_text: str = "Open link", body_text: str = "Please tap the button below."):
    """Send a CTA URL button with custom body text and button label."""
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_number,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": body_text},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": button_text[:20],  # WhatsApp max 20 chars
                        "url": link
                    }
                }
            }
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"HTTP error in send_whatsapp_interactive_link_with_text: {e}")
        return None
    return response.json()


async def send_whatsapp_interactive_link(recipient_number: str, link: str):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        
        payload = {
        "messaging_product": "whatsapp",
        "to": recipient_number,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {
                "text": f"Please click the button below to open the link."
            },
            "action": {
            "name": "cta_url",
            "parameters": {
                "display_text": "Open link",
                "url": link
            }
    }
        }
    }
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()

    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e}")
        return None

    return response.json()





async def send_whatsapp_card(recipient_id: str, card_data: dict):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }

        title = card_data.get("title") or card_data.get("category") or card_data.get("service_item")
        button_title = card_data.get("button", "See options")
        description = card_data.get("description") or card_data.get("Description")
        # picture = card_data.get("picture", "")

        button_id = title.replace(" ", "_")

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "header": {"type": "text", "text": title},
                "body": {"text": description},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": button_id, "title": button_title}}
                    ]
                },
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()
            print(f"✅ WhatsApp API Response: {response.status_code}, {response.text}")

    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP Error sending WhatsApp card: {e.response.status_code}, {e.response.text}")
    except Exception as e:
        print(f"❌ Unexpected Error sending WhatsApp card: {e}")
        


async def send_whatsapp_card_with_link(recipient_id: str, card_data: dict):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }

        title = card_data.get("title") or card_data.get("category") or card_data.get("service_item")
        button_title = card_data.get("button", "See options")
        link = card_data.get("link", "No links available")
        description = card_data.get("Description", "No description available")

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "header": {"type": "text", "text": title},
                "body": {"text": description},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": button_title,
                        "url": link
                    },
                },
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()
            print(f"✅ WhatsApp API Response: {response.status_code}, {response.text}")

    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP Error sending WhatsApp card: {e.response.status_code}, {e.response.text}")
    except Exception as e:
        print(f"❌ Unexpected Error sending WhatsApp card: {e}")




async def send_whatsapp_list_message(recipient_id: str, card_data: dict):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }

        title = card_data.get("title", "Untitled")
        description = card_data.get("description", "No description available")

        if len(description) > 72:
            description = description[:69] + "..."

        sections = [
            {
                "title": "Available Options",
                "rows": [
                    {
                        "id": option["id"],
                        "title": option["service_title"],
                        "description": option["service_description"][:69] + "..."
                        if len(option["service_description"]) > 72
                        else option["service_description"]
                    }
                    for option in card_data.get("sections", [])
                ],
            }
        ]

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": title},
                "body": {"text": description},
                "footer": {"text": "Tap on an View Options"},
                "action": {
                    "button": "View Options",
                    "sections": sections,
                },
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()
            print(f"✅ WhatsApp API Response: {response.status_code}, {response.text}")

    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP Error sending WhatsApp list: {e.response.status_code}, {e.response.text}")
    except Exception as e:
        print(f"❌ Unexpected Error sending WhatsApp list: {e}")

async def send_whatsapp_subcategory_list_message(recipient_id: str, card_data: dict, category_name: str):
    """Send secondary list for subcategories"""
    try:
        headers = {"Authorization": f"Bearer {settings.access_token}", "Content-Type": "application/json"}
        sections = [{
            "title": f"{category_name} - Subcategories",
            "rows": [{
                "id": f"subcat_{item['subcategory'].lower().replace(' ', '_')}",
                "title": item["subcategory"][:24],
                "description": item["description"][:69] + "..." if len(item["description"]) > 72 else item["description"]
            } for item in card_data]
        }]
        payload = {
            "messaging_product": "whatsapp", "to": recipient_id, "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": f"📍 *{category_name}*\nSelect a sub-category below to explore our services."},
                "footer": {"text": "Tap to view subcategories"},
                "action": {"button": "Select Option", "sections": sections}
            }
        }
        async with httpx.AsyncClient() as client:
            await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
    except Exception as e:
        print(f"❌ Error sending Subcategory list: {e}")

async def send_whatsapp_service_list_message(recipient_id: str, card_data: dict, subcategory_name: str):
    """Send tertiary list for service items (the 'Book' list)"""
    try:
        headers = {"Authorization": f"Bearer {settings.access_token}", "Content-Type": "application/json"}
        sections = [{
            "title": f"Services for {subcategory_name}",
            "rows": [{
                "id": f"service_{item['title'].lower().replace(' ', '_')}",
                "title": item["title"][:24],
                "description": f"💰 IDR {item['button']} | Tap to Book Now"
            } for item in card_data]
        }]
        payload = {
            "messaging_product": "whatsapp", "to": recipient_id, "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": f"🛎️ *{subcategory_name} - Available Services*\nChoose a service below to start your booking."},
                "footer": {"text": "GINI Bali - Instant Booking ✨"},
                "action": {"button": "Book Now", "sections": sections}
            }
        }
        async with httpx.AsyncClient() as client:
            await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
    except Exception as e:
        print(f"❌ Error sending Service list: {e}")

async def send_ai_whatsapp_list_message(
    recipient_id: str,
    menu_data: Dict[str, Any],
    button_text: str = "View Options",
    footer_text: str = "Powered by GINI Bali ✨"
) -> bool:
    try:
        image_url = menu_data.get("image_url")
        title = str (menu_data.get("title", "Our Services"))
        body_text = (
            f"✨ *{title}*\n\n"
            f"We found some great services for you! Tap the button below to view the full details and explore available options."
        )
    
        raw_sections: List[Dict] = menu_data.get("sections", [])
        all_rows = []
        for section in raw_sections:
            all_rows.extend(section.get("rows", []))

        if not all_rows:
            print("⚠️ No rows found in menu_data – skipping list message")
            return False

        whatsapp_rows = []
        for idx, item in enumerate(all_rows):
            row_id = item.get("id")
            if not row_id:
                row_id = f"row_{idx}_{recipient_id[-4:]}"  # fallback unique ID

            # WCR-13b: WhatsApp Cloud API enforces 24-char max for list row titles.
            # Smart truncation: cut at natural word boundary before hard limit.
            _raw_title = (item.get("service_title") or item.get("title") or "Untitled Option")
            if len(_raw_title) > 24:
                for _sep in [" - ", " (", ", "]:
                    _pos = _raw_title.find(_sep)
                    if 0 < _pos <= 24:
                        _raw_title = _raw_title[:_pos]
                        break
            title = _raw_title[:24]

            description = (
                item.get("service_description")
                or item.get("description")
                or ""
            )
            if len(description) > 72:
                description = description[:69] + "..."

            whatsapp_rows.append({
                "id": str(row_id),
                "title": title,
                "description": description
            })

        if len(whatsapp_rows) > 10:
            whatsapp_rows = whatsapp_rows[:10]

        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }


        # Now send the proper list message (cta_url does NOT support sections)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {
                    "text": "✨ Tap *View Options* below to browse and select your service:"
                },
                "footer": {
                    "text": footer_text[:60]
                },
                "action": {
                    "button": button_text[:20],
                    "sections": [
                        {
                            "title": "Available Services",
                            "rows": whatsapp_rows
                        }
                    ]
                }
            }
        }

        async with httpx.AsyncClient(timeout=50.0) as client:
            response = await client.post(
                f"{settings.whatsapp_api_url}",
                json=payload,
                headers=headers
            )
            response.raise_for_status()

        print(f"✅ WhatsApp List Message sent successfully to {recipient_id}")
        return True

    except httpx.HTTPStatusError as exc:
        print(f"❌ WhatsApp API error {exc.response.status_code}: {exc.response.text}")
        return False
    except Exception as exc:
        print(f"❌ Failed to send WhatsApp list message: {exc}")
        import traceback
        traceback.print_exc()
        return False
    


async def send_whatsapp_menu_list_message(recipient_id: str, card_data: dict):
    """Send a WhatsApp interactive list message.

    Accepts two dict shapes:
      Shape A (from fetch_menu_data):  {"data": [{id, title, description}, ...]}
      Shape B (from get_sub_menu):     {"main_title": ..., "main_description": ...,
                                        "items": [{category, description, ...}, ...]}
    """
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }

        title = card_data.get("main_title", "GINI Bali Menu")
        description = card_data.get("main_description", "Choose from our available services")
        if len(description) > 72:
            description = description[:69] + "..."

        rows = []
        def _safe_desc(val: object) -> str:
            s = str(val).strip() if val is not None and str(val) not in ("nan", "None", "") else ""
            return s[:69] + "..." if len(s) > 72 else s

        if "data" in card_data:
            # Shape A: pre-built list items with id/title/description
            for item in card_data["data"]:
                rows.append({
                    "id": item["id"],
                    "title": str(item.get("title", ""))[:24],
                    "description": _safe_desc(item.get("description", "")),
                })
        else:
            # Shape B: sub-menu items keyed by "category"
            for item in card_data.get("items", []):
                cat = item["category"]
                row_id = re.sub(r'[^\w]', '', cat.lower().replace(' ', '_'))
                rows.append({
                    "id": row_id,
                    "title": cat[:24],
                    "description": _safe_desc(item.get("description", "")),
                })

        # WhatsApp enforces a max of 10 rows per section — chunk automatically
        _CHUNK = 10
        if len(rows) <= _CHUNK:
            sections = [{"title": "Available Options", "rows": rows}]
        else:
            sections = [
                {"title": f"Options {_i // _CHUNK + 1}", "rows": rows[_i:_i + _CHUNK]}
                for _i in range(0, len(rows), _CHUNK)
            ]

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": title},
                "body": {"text": description},
                "footer": {"text": "Tap on an option to continue"},
                "action": {
                    "button": "View Options",
                    "sections": sections,
                },
            },
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()
            print(f"✅ WhatsApp API Response: {response.status_code}, {response.text}")

    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP Error sending WhatsApp list: {e.response.status_code}, {e.response.text}")
    except Exception as e:
        print(f"❌ Unexpected Error sending WhatsApp list: {e}")


async def send_calendar_flow(recipient_id: str):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }

        # Flow JSON configuration
        flow_json = {
            "version": "6.3",
            "data_api_version": "3.0",
            "screens": [
                {
                    "id": "DATE_PICKER_SCREEN",
                    "terminal": True,
                    "layout": {
                        "type": "SingleColumnLayout",
                        "children": [
                            {
                                "type": "CalendarPicker",
                                "name": "selected_date",
                                "label": "Select Appointment Date",
                                "helper-text": "Choose today or a future date",
                                "required": True,
                                "mode": "single",
                                "min-date": "${date.today}",
                                "include-days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                            },
                            {
                                "type": "Footer",
                                "label": "Confirm Date",
                                "on-click-action": {
                                    "name": "complete_flow"
                                }
                            }
                        ]
                    }
                }
            ]
        }

        # Construct payload
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "body": {"text": "Please select a date"},
                "flow_json": flow_json
            }
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.whatsapp_api_url,
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            print(f"✅ Calendar flow sent to {recipient_id}: {response.status_code}")

    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP Error sending calendar flow: {e.response.status_code}")
        print(f"Response content: {e.response.text}")
    except Exception as e:
        print(f"❌ Unexpected error sending calendar flow: {str(e)}")
        raise  # Re-raise for potential upstream handling


async def starting_message(recipient_number: str):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }

        # Base rows — always visible to all WhatsApp users
        # WA-MENU-EMOJI-01 (2026-09-10): each title is prefixed with an emoji
        # per Adam/Clay's Code of Conduct doc, "Rename Menu List with Emoji"
        # (WhatsApp only — the website sidebar uses its own SVG icon files
        # and is explicitly out of scope for this change). "Discount &
        # Promotions" and "Report Issue/Amenities" are shortened to "Discounts
        # & Promos" / "Report Issue" once prefixed, to stay within WhatsApp's
        # 24-character hard limit on list row titles (both were already at or
        # near that limit before adding 3 more characters for the emoji).
        # `id` values are untouched — every id is matched verbatim elsewhere
        # in this file's list_reply/nfm_reply handlers.
        _base_rows = [
            {"id": "order_services",       "title": "🛎️ Order Services",       "description": "Book transport, massage, food & more"},
            {"id": "bali_handbook",        "title": "📖 Bali Handbook",         "description": "Essential Bali travel information"},
            {"id": "recommendations",      "title": "💡 Recommendations",       "description": "Best places to eat, visit & explore"},
            {"id": "plan_my_trip",         "title": "🗺️ Plan My Trip",          "description": "Get a personalised Bali itinerary"},
            {"id": "what_to_do_today",     "title": "☀️ What To Do Today?",     "description": "Find activities based on your mood"},
            {"id": "discount__promotions", "title": "🏷️ Discounts & Promos",    "description": "Exclusive deals and offers"},
            {"id": "voice_translator",     "title": "🗣️ Voice Translator",      "description": "Translate & learn Bahasa Indonesia"},
            {"id": "currency_converter",   "title": "💱 Currency Converter",    "description": "Live currency conversion rates"},
        ]

        # Villa-only rows — only shown to guests who have linked their villa via QR scan or registration
        _villa_rows = [
            {"id": "passport_submission",       "title": "📝 Passport Submission", "description": "Submit your passport for villa check-in"},
            {"id": "report_issue_or_amenities", "title": "🛠️ Report Issue",        "description": "Report a problem or request items"},
        ]

        _villa_code = await get_user_villa_code(recipient_number)
        _rows = _base_rows + (_villa_rows if _villa_code else [])

        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_number,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {
                    "type": "text",
                    "text": "🌴 Welcome to GINI Bali! 🌴"
                },
                "body": {
                    "text": (
                        "I’m here to assist you with anything you need during your Bali experience. "
                        "Whether it’s ordering services like transportation, massage, or food, or finding "
                        "the best spots to visit, I’ve got you covered!\n\n"
                        "Choose an option below to get started 👇"
                    )
                },
                "footer": {"text": "Tap on an option to continue"},
                "action": {
                    "button": "View Options",
                    "sections": [
                        {
                            "title": "Main Menu",
                            "rows": _rows,
                        }
                    ]
                }
            }
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()

    except httpx.HTTPStatusError as e:
        print(f"HTTP error occurred: {e}")
        return None

    return response.json()



def _parse_checkin_reply(text: str) -> datetime.datetime | None:
    """
    Parse a natural check-in date reply from the guest.
    Accepts formats like: "April 2", "2 April", "02/04", "April 2 3pm",
    "2/4/2026", "today", "tomorrow".
    Returns a datetime (defaulting to noon if no time given), or None if unparseable.
    """
    import re
    text = text.strip().lower()
    now = datetime.datetime.now()

    if text in ("today", "now"):
        return now.replace(hour=12, minute=0, second=0, microsecond=0)
    if text == "tomorrow":
        return (now + datetime.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)

    # Extract optional hour (e.g. "3pm", "15:00", "3 pm")
    hour = 12
    minute = 0
    time_match = re.search(r'(\d{1,2})\s*:\s*(\d{2})', text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        text = text[:time_match.start()].strip()
    else:
        ampm_match = re.search(r'(\d{1,2})\s*(am|pm)', text)
        if ampm_match:
            hour = int(ampm_match.group(1))
            if ampm_match.group(2) == 'pm' and hour != 12:
                hour += 12
            elif ampm_match.group(2) == 'am' and hour == 12:
                hour = 0
            text = text[:ampm_match.start()].strip()

    MONTHS = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
        'january': 1, 'february': 2, 'march': 3, 'april': 4, 'june': 6,
        'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
    }

    # "April 2" or "2 April"
    for m_name, m_num in MONTHS.items():
        pat1 = re.search(rf'{m_name}\s+(\d{{1,2}})', text)
        pat2 = re.search(rf'(\d{{1,2}})\s+{m_name}', text)
        match = pat1 or pat2
        if match:
            day = int(match.group(1))
            year = now.year if now.month <= m_num else now.year + 1
            try:
                return datetime.datetime(year, m_num, day, hour, minute)
            except ValueError:
                return None

    # "DD/MM" or "MM/DD" or "DD/MM/YYYY"
    slash_match = re.search(r'(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?', text)
    if slash_match:
        a, b = int(slash_match.group(1)), int(slash_match.group(2))
        yr = int(slash_match.group(3)) if slash_match.group(3) else now.year
        if yr < 100:
            yr += 2000
        # Prefer DD/MM (Bali guests are mostly international, ISO-style)
        try:
            return datetime.datetime(yr, b, a, hour, minute)
        except ValueError:
            try:
                return datetime.datetime(yr, a, b, hour, minute)
            except ValueError:
                return None

    # Just a number — treat as day of current month
    just_day = re.fullmatch(r'\d{1,2}', text.strip())
    if just_day:
        day = int(just_day.group())
        try:
            return datetime.datetime(now.year, now.month, day, hour, minute)
        except ValueError:
            return None

    return None


async def start_onboarding(sender_id: str, villa_code: str):
    """Ask the guest their name first, then check-in date — first step of post-arrival onboarding."""
    onboarding_sessions[sender_id] = {
        "step": "awaiting_name",
        "villa_code": villa_code,
        "timestamp": datetime.datetime.now(),
    }
    await send_whatsapp_message(
        sender_id,
        "👋 *Welcome! I'm EASY, your personal Bali concierge.*\n\n"
        "To personalise your experience, may I know your *full name*?\n\n"
        "Just reply with your name, e.g. *John Smith*"
    )


async def perform_arrival_confirmation(sender_id: str, villa_code: str, customer_id: str = None):
    """Logs check-in and notifies villa manager."""
    try:
        from app.db.session import db
        villa_info = await get_villa_info_by_code(villa_code)
        villa_name = (villa_info or {}).get("name") or "the villa"

        if not customer_id:
            customer_id = await get_or_create_customer(sender_id)

        # 1. Update customer record
        await customer_collection.update_one(
            {"phone": sender_id},
            {"$set": {"villa_code": villa_code, "last_active": datetime.datetime.now()}}
        )

        checkin_data = {
            "sender_id": sender_id,
            "customer_id": customer_id,
            "villa_code": villa_code,
            "villa_name": villa_name,
            "location_zone": (villa_info or {}).get("location") or "Bali",
            "checkin_time": datetime.datetime.now(),
            "status": "active"
        }
        await db["checkins"].update_one(
            {"sender_id": sender_id, "status": "active"},
            {"$set": checkin_data},
            upsert=True
        )

        # 2. Fetch rich profile for manager contact
        rich_profile = await db["villa_profiles"].find_one({"villa_code": villa_code})
        rp = rich_profile or {}

        # 3. Notify villa manager
        manager_num = rp.get("manager_phone") or (villa_info or {}).get("manager_number", "")
        if manager_num:
            clean_mgr = "".join(filter(str.isdigit, str(manager_num)))
            if clean_mgr:
                try:
                    await send_whatsapp_message(
                        clean_mgr,
                        f"🔔 *New Guest Arrival!*\n\n"
                        f"Guest `{sender_id}` has just checked into *{villa_name}*.\n"
                        f"Concierge access is now active."
                    )
                except Exception as _mgr_err:
                    logger.warning(f"Manager notification failed for arrival {villa_code}: {_mgr_err}")

        return True
    except Exception as e:
        logger.error(f"Error in perform_arrival_confirmation for {sender_id}: {e}")
        return False


async def ensure_active_checkin(sender_id: str, villa_code: str, villa_name: str = "", location_zone: str = ""):
    """Idempotently create/refresh an active check-in record for a guest.

    The automated guest sequences (passport reminder, day-1 welcome, mid-stay,
    etc.) in automation_butler.py iterate `checkins.find({status: "active"})`.
    Without an active record, none of them fire. This helper is called at
    Venue Setup Flow completion — the current onboarding path — so those
    sequences have the trigger record they depend on.

    Keyed on (sender_id, status="active") with upsert so it never duplicates.
    `checkin_time` is set only on insert ($setOnInsert) so re-entering the flow
    never resets the guest's stay clock. Non-fatal — any failure is logged and
    swallowed; onboarding must never break because a check-in write failed.
    """
    try:
        from app.db.session import db
        await db["checkins"].update_one(
            {"sender_id": sender_id, "status": "active"},
            {
                "$set": {
                    "sender_id": sender_id,
                    "villa_code": villa_code,
                    "villa_name": villa_name or "",
                    "location_zone": location_zone or "",
                    "status": "active",
                },
                "$setOnInsert": {"checkin_time": datetime.datetime.now()},
            },
            upsert=True,
        )
        return True
    except Exception as e:
        logger.error(f"ensure_active_checkin failed (non-fatal) for {sender_id}: {e}")
        return False


async def log_guest_inquiry(sender_id: str, villa_code: str, query: str, response: str, intent: str = "general", customer_id: str = None):
    """Logs a guest's question/interaction to the database for oversight."""
    try:
        if not customer_id:
            customer_id = await get_or_create_customer(sender_id)
        inquiry_doc = {
            "sender_id": sender_id,
            "customer_id": customer_id,
            "villa_code": villa_code,
            "query": query,
            "response": response,
            "intent": intent,
            "timestamp": datetime.datetime.now(),
            "status": "responded"
        }
        await inquiry_collection.insert_one(inquiry_doc)
        logger.info(f"Logged inquiry from {sender_id} at {villa_code}")
        
        # If it's a support request, notify manager immediately
        if intent == "support_request":
            villa_info = await get_villa_info_by_code(villa_code)
            if villa_info and villa_info.get("manager_number"):
                mgr_num = "".join(filter(str.isdigit, str(villa_info["manager_number"])))
                mgr_msg = (
                    f"🆘 *Support Requested!*\n\n"
                    f"Villa: *{villa_info.get('name', 'GINI Bali')}*\n"
                    f"Guest ID: `{sender_id[-4:]}`\n"
                    f"The guest has requested to speak with us. Please respond as soon as possible."
                )
                await send_whatsapp_message(mgr_num, mgr_msg)
    except Exception as e:
        logger.error(f"Failed to log inquiry: {e}")

async def attempt_sp_reassignment(order_num: str):
    """
    After an SP declines, find the next available SP for the same service and re-offer.
    If no SPs remain, notify the guest and admin.
    """
    try:
        order = await order_collection.find_one({"order_number": order_num})
        if not order:
            return

        service_name = order.get("service_name", "")
        guest_id     = order.get("sender_id", "")
        declined_by  = set(order.get("declined_by", []))

        # Get all SPs for this service, considering location
        villa_code = order.get("villa_code", "")
        location_zone = order.get("location_zone")
        if not location_zone and villa_code:
            from app.services.menu_services import get_villa_location_by_code
            location_zone = await get_villa_location_by_code(villa_code)

        all_sp_numbers = await fetch_whatsapp_numbers(service_name, location_zone)
        remaining = [n for n in all_sp_numbers if n not in declined_by]

        if remaining:
            # Reset order to pending and re-offer to remaining SPs
            await order_collection.update_one(
                {"order_number": order_num},
                {"$set": {"status": "pending"}}
            )
            # Notify guest
            if guest_id:
                await send_whatsapp_message(
                    guest_id,
                    f"⏳ We're finding another provider for your *{service_name}* booking. "
                    f"We'll confirm shortly!"
                )
            for sp_num in remaining:
                try:
                    await send_whatsapp_order_to_SP(sp_num, order)
                except Exception as e:
                    logger.error(f"Reassignment: failed to notify {sp_num}: {e}")
            logger.info(f"Reassignment: order {order_num} re-offered to {remaining}")
        else:
            # No SPs left — mark unserviceable, notify guest and admin
            await order_collection.update_one(
                {"order_number": order_num},
                {"$set": {"status": "no_providers"}}
            )
            if guest_id:
                await send_whatsapp_message(
                    guest_id,
                    f"😔 We're sorry — no providers are currently available for your *{service_name}* request.\n\n"
                    f"Our team has been alerted and will contact you shortly to assist."
                )
            admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
            await send_whatsapp_message(
                admin_number,
                f"🚨 *Order {order_num} has no available SPs*\n\n"
                f"Service: *{service_name}*\n"
                f"Guest: `{guest_id[-4:] if guest_id else 'unknown'}`\n\n"
                f"All providers have declined. Manual reassignment required."
            )
            logger.warning(f"Reassignment: order {order_num} — no providers left, admin notified")
    except Exception as e:
        logger.error(f"attempt_sp_reassignment error for {order_num}: {e}")


async def send_decline_confirmation(recipient_number: str, order_num: str):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_number,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": "Are you sure you want to decline this request? This action cannot be undone.\nApakah Anda yakin ingin menolak permintaan ini? Tindakan ini tidak dapat dibatalkan."
                },
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"confirm_decline_{order_num}",
                                "title": "Yes, Decline"
                            }
                        },
                        {
                            "type": "reply",
                            "reply": {
                                "id": f"cancel_decline_{order_num}",
                                "title": "No, Go Back"
                            }
                        }
                    ]
                }
            }
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        print(f"Error sending decline confirmation: {e}")
        return None

async def get_user_villa_code(sender_id: str):
    try:
        user_data = await villa_code_collection.find_one({"sender_id": sender_id})
        return user_data.get("villa_code") if user_data else None
    except Exception as e:
        print(f"Error getting villa code for {sender_id}: {e}")
        return None

async def save_user_villa_code(sender_id: str, villa_code: str, source: str = "manual_entry"):
    """
    Save villa_code for a sender_id to villa_code_collection (used by resolve_customer_context Step 1).

    source values:
        "qr_scan"      -- guest scanned the villa QR link ("Hi, I am in Villa X")
        "registration" -- saved during WA registration flow (NativeFlowMessage reg form)
        "manual_entry" -- guest typed their V-code in response to villa_code_sessions ask
    """
    try:
        now = datetime.datetime.now()
        await villa_code_collection.update_one(
            {"sender_id": sender_id},
            {
                "$set": {
                    "sender_id":    sender_id,
                    "villa_code":   villa_code,
                    "source":       source,
                    "verified_at":  now,
                    "updated_at":   now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True
        )
        return True
    except Exception as e:
        print(f"Error saving villa code for {sender_id}: {e}")
        return False

async def _resolve_villa_and_guest_for_passport(sender_id: str):
    """
    WCR-VILLA-MISMATCH-01 (2026-08-25): shared villa_code + guest_id resolution
    for WhatsApp passport uploads, used by every entry point (the normal
    passport_sessions flow and the pending_media_passport confirmation flow).

    villa_code: villa_code_collection first (get_user_villa_code), then
    customer_collection as a fallback -- mirrors what Category Flow already
    does (handle_category_flow_init), so passport submission is never the one
    path that misses a villa a guest only confirmed via registration/booking.

    guest_id: best-effort lookup by phone in guest_profile_collection, so a
    guest who resubmits their passport (e.g. after a rejection) is recognised
    as the same person across submissions -- matches what the web passport
    upload path already stores. None here just means "not registered yet";
    it never blocks the upload (villa_code is the real gate).

    guest_name (PASSPORT-WA-NAME-01, 2026-08-31, live report -- Clay/Adam):
    "if the passport is rejected, the name does not appear, but a different
    name such as 'WHATSAPP GUEST 1660' shows instead." Root cause traced to
    process_whatsapp_passport() always falling back to
    f"WhatsApp Guest {sender_id[-4:]}" because BOTH call sites never passed
    a real name -- even though the guest_profile lookup right above already
    fetches the full document (and therefore full_name) whenever the guest
    is already known from a prior registration/booking (same phone number,
    per customer profiling). This was never an accept-vs-reject bug -- both
    outcomes read the same passport.guest_name field; it was simply never
    populated with a real name for WhatsApp-sourced uploads at all.
    """
    villa_code = await get_user_villa_code(sender_id)
    if not villa_code:
        try:
            _cc_doc = await customer_collection.find_one({"phone": sender_id})
            villa_code = (_cc_doc or {}).get("villa_code") or ""
        except Exception as _cc_err:
            logger.warning(f"customer_collection fallback lookup failed for passport ({sender_id}): {_cc_err}")
    villa_code = villa_code or "UNKNOWN"

    guest_id = None
    guest_name = None
    try:
        _gp = await guest_profile_collection.find_one({"phone_number": sender_id})
        guest_id = (_gp or {}).get("guest_id")
        guest_name = (_gp or {}).get("full_name") or None
    except Exception as _gp_err:
        logger.warning(f"guest_profile_collection lookup failed for passport ({sender_id}): {_gp_err}")

    return villa_code, guest_id, guest_name


def is_valid_villa_code(villa_code: str):
    pattern = r'^V\d+$'
    return bool(re.match(pattern, villa_code.upper()))

async def send_villa_code_request(sender_id: str):
    """Send villa code request message"""
    message = (
        "🏝️ Welcome to GINI Bali! 🏝️\n\n"
        "To provide you with the best service, please share your Villa Code.\n\n"
        "Your Villa Code should be in the format: V1, V2, etc.\n\n"
        "Please enter your Villa Code:"
    )
    await send_whatsapp_message(sender_id, message)


async def send_invoice_with_download(sender_id: str, download_url: str, order_number: str):
    """
    Send invoice to guest — CTA button first (best UX), template fallback on failure.

    CTA URL interactive messages are rejected by Meta outside the 24-hr window.
    Any failure triggers the invoice_ready_guest template so the guest always
    receives their receipt link.
    """
    from app.utils.notification_logger import log_notification_attempt

    # Use the permanent receipt link (never expires) instead of the raw
    # 24-hour S3 presigned URL. The /invoice/{order}/download endpoint mints a
    # fresh signed URL on each request. Fall back to the passed URL only if
    # BASE_URL is not configured.
    _base = (getattr(settings, "BASE_URL", "") or "").rstrip("/")
    permanent_url = f"{_base}/invoice/{order_number}/download" if _base else download_url

    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "header": {"type": "text", "text": "📄 Your Invoice is Ready!"},
            "body": {
                "text": (
                    f"Thank you for your payment! Your invoice for order {order_number} "
                    f"is now available for download.\n\n"
                    f"✅ Payment confirmed\n📄 Invoice generated\n⬇️ Click below to download"
                )
            },
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": "Download Invoice", "url": permanent_url},
            },
        },
    }

    # Attempt 1: CTA URL interactive message (tappable button, works inside 24-hr window)
    cta_ok = False
    meta_code = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            if response.status_code == 200:
                cta_ok = True
                logger.info(f"Invoice CTA sent to {sender_id} for order {order_number}")
            else:
                try:
                    meta_code = response.json().get("error", {}).get("code")
                    if meta_code is not None:
                        meta_code = int(meta_code)
                except Exception:
                    pass
                logger.error(
                    f"Invoice CTA failed for {sender_id} order {order_number}: "
                    f"HTTP {response.status_code} meta_code={meta_code}"
                )
    except Exception as e:
        logger.error(f"Invoice CTA exception for {sender_id} order {order_number}: {e}")

    await log_notification_attempt(
        recipient=sender_id,
        notification_type="invoice_ready_guest",
        channel="cta_interactive",
        success=cta_ok,
        order_number=order_number,
        meta_error_code=meta_code,
    )

    if cta_ok:
        return True

    # Attempt 2: approved template — bypasses 24-hr window
    logger.info(
        f"[Invoice] CTA failed for {sender_id}, retrying with template "
        f"'invoice_ready_guest' for order {order_number}"
    )
    template_ok = await send_whatsapp_template(
        sender_id,
        "invoice_ready_guest",
        [order_number, permanent_url],
    )
    await log_notification_attempt(
        recipient=sender_id,
        notification_type="invoice_ready_guest",
        channel="template",
        success=template_ok,
        order_number=order_number,
        template_name="invoice_ready_guest",
        error_detail="cta_fallback",
    )
    if not template_ok:
        logger.error(f"[Invoice] BOTH CTA and template failed for {sender_id} order {order_number}")
    return template_ok


async def send_whatsapp_flow_message(recipient_id: str, order_number: str):
    """Send WhatsApp Flow message to user"""
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        
        FLOW_ID = "24190558223942158"  # Your Flow ID
        
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "header": {
                    "type": "text",
                    "text": "📅 Appointment Booking"
                },
                "body": {
                    "text": f"Please select your preferred appointment date for order #{order_number}.\n\nTap the button below to open the date picker."
                },
                "footer": {
                    "text": "GINI Bali Services"
                },
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": "3",
                        "flow_token": order_number,
                        "flow_id": FLOW_ID,
                        "flow_cta": "Select Date",
                        "flow_action": "navigate"
                    }
                }
            }
        }
    
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            
            if response.status_code != 200:
                print(f"❌ WhatsApp API error: {response.status_code}")
                print(f"❌ Response: {response.text}")
                return None
                
            response.raise_for_status()
            result = response.json()
            print(f"✅ Flow message sent successfully to {recipient_id}")
            print(f"📱 Message ID: {result.get('messages', [{}])[0].get('id', 'N/A')}")
            return result
            
    except httpx.TimeoutException:
        print(f"❌ Timeout error sending flow message to {recipient_id}")
        return None
    except Exception as e:
        print(f"❌ Error sending flow message: {e}")
        import traceback
        traceback.print_exc()
        return None
    

async def send_whatsapp_service_flow_message(recipient_id: str, flow_token: str):
    """Send WhatsApp Service Selection Flow message to user"""
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        
        # ⚠️ VERIFY THIS IS THE CORRECT FLOW ID
        SERVICE_FLOW_ID = "2282521258887998" 
        
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "header": {
                    "type": "text",
                    "text": "🛍️ Service Selection"
                },
                "body": {
                    "text": "Please select your preferred service from our available options.\n\nTap the button below to browse and select your service."
                },
                "footer": {
                    "text": "GINI Bali Services"
                },
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": "3",
                        "flow_token": flow_token,
                        "flow_id": SERVICE_FLOW_ID,
                        "flow_cta": "Select Service",
                        "flow_action": "data_exchange"
                    }
                }
            }
        }
        
        print(f"🔍 DEBUG: Full payload: {json.dumps(payload, indent=2)}")
    
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            
            print(f"🔍 DEBUG: WhatsApp API response status: {response.status_code}")
            print(f"🔍 DEBUG: WhatsApp API response: {response.text}")
            
            if response.status_code != 200:
                print(f"❌ WhatsApp API error: {response.status_code}")
                print(f"❌ Response: {response.text}")
                return None
                
            response.raise_for_status()
            result = response.json()
            print(f"✅ Service flow message sent successfully to {recipient_id}")
            print(f"📱 Message ID: {result.get('messages', [{}])[0].get('id', 'N/A')}")
            return result
            
    except Exception as e:
        print(f"❌ Error sending service flow message: {e}")
        import traceback
        traceback.print_exc()
        return None
    

async def send_whatsapp_order_flow_message(recipient_id: str, flow_token: str):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        
        # ⚠️ VERIFY THIS IS THE CORRECT FLOW ID
        SERVICE_FLOW_ID = "1343770641089909" 
        
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "header": {
                    "type": "text",
                    "text": "🛍️ GINI Bali Services"
                },
                "body": {
                    "text": "Please select your preferred service from our available options.\n\nTap the button below to browse and select your service."
                },
                "footer": {
                    "text": "GINI Bali Catelog"
                },
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": "3",
                        "flow_token": flow_token,
                        "flow_id": SERVICE_FLOW_ID,
                        "flow_cta": "Select Service",
                        "flow_action": "data_exchange"
                    }
                }
            }
        }
    
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            
            print(f"🔍 DEBUG: WhatsApp API response status: {response.status_code}")
            print(f"🔍 DEBUG: WhatsApp API response: {response.text}")
            
            if response.status_code != 200:
                print(f"❌ WhatsApp API error: {response.status_code}")
                print(f"❌ Response: {response.text}")
                return None
                
            response.raise_for_status()
            result = response.json()
            print(f"✅ Service flow message sent successfully to {recipient_id}")
            print(f"📱 Message ID: {result.get('messages', [{}])[0].get('id', 'N/A')}")
            return result
            
    except Exception as e:
        print(f"❌ Error sending service flow message: {e}")
        import traceback
        traceback.print_exc()
        return None
    



async def send_ai_whatsapp_order_flow_message(recipient_id: str, flow_token: str, menu_data: Dict[str, Any]):
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json"
        }
        
        SERVICE_FLOW_ID = "707681682414552"

        image_url = menu_data.get("image_url")
        title = str(menu_data.get("title", "Our Services"))
        body_text = (
            f"Ready to enjoy our premium *{title}* experience. Select your preferred item below."
        )

        # Extract service items from rows
        rows = menu_data.get("sections", [{}])[0].get("rows", [])
        service_items = []
        for row in rows:
            service_items.append({
                "id": row.get("id"),
                "title": row.get("title"),
                "description": row.get("description"),
                "metadata": row.get("price")
            })
        
        # Get today's date for calendar
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "header": {
                    "type": "image",
                    "image": {
                        "link": image_url
                    }
                },
                "body": {
                    "text": body_text
                },
                "footer": {
                    "text": "Powered by GINI Bali ✨"
                },
                "action": {
                    "name": "flow",
                    "parameters": {
                        "flow_message_version": "3",
                        "flow_token": flow_token,
                        "flow_id": SERVICE_FLOW_ID,
                        "flow_cta": "Select Service",
                        "flow_action": "navigate", 
                        "flow_action_payload": {  
                            "screen": "SERVICE_AND_DATE_SELECTION",
                            "data": {
                                "service_items": service_items,
                                "min_date": today,
                                "today_date": today,
                                "flow_token": flow_token
                            }
                        }
                    }
                }
            }
        }
    
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            
            print(f"🔍 DEBUG: WhatsApp API response status: {response.status_code}")
            print(f"🔍 DEBUG: WhatsApp API response: {response.text}")
            
            if response.status_code != 200:
                print(f"❌ WhatsApp API error: {response.status_code}")
                print(f"❌ Response: {response.text}")
                return None
                
            response.raise_for_status()
            result = response.json()
            print(f"✅ Service flow message sent successfully to {recipient_id}")
            print(f"📱 Message ID: {result.get('messages', [{}])[0].get('id', 'N/A')}")
            return result
            
    except Exception as e:
        print(f"❌ Error sending service flow message: {e}")
        import traceback
        traceback.print_exc()
        return None




def extract_villa_name(text: str):
    # Robust extraction: Capture everything after "Hi, I am in"
    match = re.search(r"Hi,\s*I\s*am\s*in\s+(.+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    
    # Fallback to the old "word after villa" logic if regex fails
    words = text.split()
    for i, word in enumerate(words):
        if word.lower() == "villa" and i + 1 < len(words):
            return f"Villa {words[i + 1]}"
    return None


async def get_ai_chatbot_response(query: str, user_id: str) -> Optional[str]:
    """
    Calls the AI chatbot endpoint to generate a response
    """
    try:
        async with httpx.AsyncClient(timeout=50.0) as client:
            response = await client.post(
                f"{settings.BASE_URL}/chatbot/generate-response",
                json={"query": query},
                params={"user_id": user_id}
            )
            response.raise_for_status()
            data = response.json()
            return data.get("response") or data.get("message")
    except Exception as e:
        print(f"Error calling AI chatbot: {e}")
        return None
    
async def send_whatsapp_image_with_caption(recipient_id: str, image_url: str, caption: str):
    """Send image with caption via WhatsApp"""
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "type": "image",
            "image": {
                "link": image_url,
                "caption": caption
            }
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.whatsapp_api_url, 
                json=payload, 
                headers=headers
            )
            response.raise_for_status()
            print(f"✅ Image sent to {recipient_id}")
            
    except Exception as e:
        print(f"❌ Error sending image: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Admin WhatsApp Commands
# Authorised numbers: ADMIN_PHONE_NUMBERS env var (comma-separated digits).
#
# Commands (case-insensitive, leading/trailing whitespace ignored):
#   REASSIGN <order_number> <sp_code>          – reassign order to a new SP
#   REFUND <order_number> [reason text]        – initiate Xendit refund for a PAID order
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin(sender_id: str) -> bool:
    """Return True if sender_id is in the configured admin phone list."""
    raw = settings.ADMIN_PHONE_NUMBERS or ""
    admin_set = {n.strip() for n in raw.split(",") if n.strip()}
    return sender_id in admin_set


async def _handle_admin_command(sender_id: str, text: str) -> bool:
    """Parse and execute admin commands.  Returns True if the text was an admin command."""
    upper = text.strip().upper()

    # ── REASSIGN <order_number> <sp_code> ──────────────────────────────────────
    if upper.startswith("REASSIGN "):
        parts = text.strip().split()
        if len(parts) < 3:
            await send_whatsapp_message(sender_id, "Usage: REASSIGN <order_number> <sp_code>\nExample: REASSIGN EB123 S4")
            return True
        order_num = parts[1].upper()
        new_sp_code = parts[2].upper()

        order_doc = await order_collection.find_one({"order_number": order_num})
        if not order_doc:
            await send_whatsapp_message(sender_id, f"❌ Order {order_num} not found.")
            return True

        current_status = order_doc.get("status", "")
        if current_status in ("cancelled", "REFUNDED"):
            await send_whatsapp_message(sender_id, f"❌ Cannot reassign order {order_num} — status is {current_status}.")
            return True

        # Look up new SP's WhatsApp number
        from app.services.menu_services import get_service_providers as _get_sps
        sp_df = await _get_sps()
        sp_row = sp_df[sp_df["Number"].str.upper() == new_sp_code]
        if sp_row.empty:
            available = sp_df["Number"].dropna().tolist()
            await send_whatsapp_message(sender_id, f"❌ SP code '{new_sp_code}' not found.\nAvailable: {', '.join(available)}")
            return True

        new_sp_whatsapp = str(sp_row.iloc[0].get("WhatsApp", "")).strip()
        new_sp_name = str(sp_row.iloc[0].get("Name", new_sp_code)).strip()
        old_sp_code = order_doc.get("service_provider_code", "none")

        # ── FSM REASSIGNMENT SURGERY ─────────────────────────────────────────
        from app.services.status_service import BookingStatusManager
        fsm_res = await BookingStatusManager.process_reassignment(
            order_number=order_num,
            changed_by=f"ADMIN:{sender_id}",
            reason=f"Reassigned from {old_sp_code} to {new_sp_code}"
        )

        if not fsm_res["success"]:
            await send_whatsapp_message(sender_id, f"❌ Reassignment failed: {fsm_res['error']}")
            return True

        # Assign the NEW SP code (process_reassignment cleared it)
        await order_collection.update_one(
            {"order_number": order_num},
            {"$set": {"service_provider_code": new_sp_code}}
        )
        
        logger.info(f"Admin {sender_id} reassigned order {order_num} from {old_sp_code} to {new_sp_code}")

        # Notify new SP with template (bypasses 24-hr window)
        if new_sp_whatsapp:
            try:
                # Refresh doc to get latest state for notification
                updated_order = await order_collection.find_one({"order_number": order_num})
                order_dict = {k: v for k, v in updated_order.items() if k != "_id"}
                
                await send_whatsapp_order_to_SP(new_sp_whatsapp, order_dict)
                await order_collection.update_one(
                    {"order_number": order_num},
                    {"$set": {"sp_notified_at": datetime.now()}}
                )
                sp_notified = f"New SP ({new_sp_name} / {new_sp_whatsapp}) notified via template."
            except Exception as _sp_err:
                logger.error(f"Error notifying new SP {new_sp_code} on reassign: {_sp_err}")
                sp_notified = f"⚠️ Could not notify new SP: {str(_sp_err)}"
        else:
            sp_notified = f"⚠️ No WhatsApp number found for {new_sp_code}."

        await send_whatsapp_message(
            sender_id,
            f"✅ *Order Reassigned*\n\n"
            f"Order: *{order_num}*\n"
            f"From SP: {old_sp_code}\n"
            f"To SP: {new_sp_code} ({new_sp_name})\n"
            f"{sp_notified}"
        )
        return True

    # ── REFUND <order_number> [reason] ─────────────────────────────────────────
    # Phase 2C-2: Refactored to use BookingStatusManager FSM.
    # Note: This command marks the order as REFUNDED in GINI Bali after manual Xendit refund.
    if upper.startswith("REFUND "):
        parts = text.strip().split(None, 2)
        if len(parts) < 2:
            await send_whatsapp_message(sender_id, "Usage: REFUND <order_number> [reason]\nExample: REFUND EB123 Guest checked out early")
            return True
        order_num = parts[1].upper()
        reason = parts[2].strip() if len(parts) > 2 else "Requested by admin"

        # Call FSM process_manual_refund
        # This handles all guards (PAID, CONFIRMED/COMPLETED), history, and field updates.
        from app.services.status_service import BookingStatusManager
        fsm_result = await BookingStatusManager.process_manual_refund(
            order_number=order_num,
            changed_by=f"ADMIN_{sender_id}",
            reason=reason
        )

        if not fsm_result["success"]:
            await send_whatsapp_message(sender_id, f"❌ Refund failed for {order_num}: {fsm_result.get('error')}")
            return True

        updated_order = fsm_result["order"]
        paid_amount = updated_order.get("payment", {}).get("paid_amount", 0)
        guest_id = updated_order.get("sender_id", "")

        logger.info(f"Admin {sender_id} marked order {order_num} as REFUNDED via FSM")

        # Notify guest
        if guest_id and str(guest_id).isdigit():
            try:
                await send_whatsapp_message(
                    guest_id,
                    f"✅ *Refund Processed*\n\n"
                    f"Your refund for *{updated_order.get('service_name', '')}* (Order {order_num}) "
                    f"of IDR {int(paid_amount):,} has been approved and marked as processed.\n\n"
                    f"Funds typically arrive within 3–5 business days depending on your bank."
                )
            except Exception:
                pass

        await send_whatsapp_message(
            sender_id,
            f"✅ *Refund Marked (FSM)*\n\n"
            f"Order: *{order_num}*\n"
            f"Amount: IDR {int(paid_amount):,}\n"
            f"Reason: {reason}\n"
            f"FSM Status: REFUNDED\n"
            f"Payment Status: REFUNDED\n"
            f"Disbursement: CANCELLED\n"
            f"Guest notified: {'Yes' if guest_id and str(guest_id).isdigit() else 'No (web order)'}"
        )
        return True

    return False  # Not an admin command


async def _handle_sp_cancellation(sender_id: str, text: str) -> bool:
    """
    SP-initiated cancellation after accepting a booking.
    Triggered by: CANCEL <order_number>

    Pre-payment (accepted / payment_pending):
      - Clears confirmed_by_provider, adds SP to declined_by
      - Auto-reassigns to next available SP
    Post-payment (PAID):
      - Flags to admin for manual refund handling
    """
    upper = text.strip().upper()
    if not upper.startswith("CANCEL "):
        return False

    parts = text.strip().split()
    if len(parts) < 2:
        return False

    order_num = parts[1].upper()
    order_doc = await order_collection.find_one({"order_number": order_num})
    if not order_doc:
        return False

    # Only act if this SP is the confirmed provider
    if order_doc.get("confirmed_by_provider") != sender_id:
        return False

    status = order_doc.get("status", "")
    service_name = order_doc.get("service_name", "Service")
    guest_id = order_doc.get("sender_id", "")

    success_states = ("PAID", "funds_distributed", "disbursement_initiated", "disbursement_pending")
    if status in success_states:
        # Post-payment: cannot auto-cancel — escalate to admin
        raw_admin = settings.ADMIN_PHONE_NUMBERS or ""
        admin_numbers = [n.strip() for n in raw_admin.split(",") if n.strip()]
        for admin_num in admin_numbers:
            await send_whatsapp_message(
                admin_num,
                f"🚨 *SP Cancel Request (Post-Payment)*\n\n"
                f"Order: *{order_num}*\n"
                f"Service: {service_name}\n"
                f"SP: {sender_id}\n\n"
                f"This order is already PAID. Manual refund may be required.\n"
                f"Use: REFUND {order_num} <reason>"
            )
        await send_whatsapp_message(
            sender_id,
            f"⚠️ *Order {order_num} is already paid.*\n\n"
            f"We've alerted our team — they will contact you and the guest shortly to resolve this."
        )
        logger.warning(f"SP {sender_id} attempted post-payment cancel on {order_num} — escalated to admin")
        return True

    # Pre-payment: clear SP and auto-reassign
    await order_collection.update_one(
        {"order_number": order_num},
        {
            "$set": {
                "confirmed_by_provider": None,
                "sp_cancelled_by": sender_id,
                "sp_cancelled_at": datetime.datetime.now(),
            },
            "$addToSet": {"declined_by": sender_id},
        }
    )
    logger.info(f"SP {sender_id} cancelled order {order_num} (pre-payment) — attempting reassignment")

    await send_whatsapp_message(
        sender_id,
        f"✅ Your cancellation for order *{order_num}* (*{service_name}*) has been noted.\n\n"
        f"We will find another provider for the guest."
    )

    # Notify guest while reassignment runs in background
    if guest_id:
        await send_whatsapp_message(
            guest_id,
            f"⏳ We're finding another provider for your *{service_name}* booking.\n"
            f"We'll confirm shortly — sorry for the delay!"
        )

    await attempt_sp_reassignment(order_num)
    return True


async def _is_sender_sp(sender_id: str) -> bool:
    try:
        from app.services.menu_services import get_service_providers
        providers_df = await get_service_providers()
        if providers_df is not None and not providers_df.empty:
            all_sp_numbers = providers_df["Number"].dropna().astype(str).str.strip().tolist()
            normalized_sender = sender_id.strip('+').lstrip('0')
            for num in all_sp_numbers:
                norm_num = num.strip('+').lstrip('0')
                if norm_num and (norm_num == normalized_sender or norm_num.endswith(normalized_sender) or normalized_sender.endswith(norm_num)):
                    return True
    except Exception as e:
        logger.warning(f"Failed to check if sender is SP: {e}")
    return False
_CIRCUIT_BREAKER_MEMORY = {}

async def process_message(sender_id: str, message_payload: dict, message_id:str):
    start_time = datetime.datetime.now()
    try:
        # Standardize sender_id (phone number)
        sender_id = re.sub(r"\s+", "", sender_id)
        
        # ── Persistent Circuit Breaker (Prevent Auto-Responder loops) ──
        try:
            from app.db.session import rate_limit_collection
            now = datetime.datetime.now(datetime.timezone.utc)
            
            # Determine limits based on message type
            is_interactive = "interactive" in message_payload
            rate_limit_max = 15 if is_interactive else 5
            window_seconds = 10
            
            # Atomic sliding window approximation (using window_start)
            doc = await rate_limit_collection.find_one_and_update(
                {
                    "_id": sender_id,
                    "window_start": {"$gt": now - datetime.timedelta(seconds=window_seconds)}
                },
                {"$inc": {"count": 1}},
                return_document=True
            )
            
            if doc:
                count = doc["count"]
            else:
                # Window expired or doesn't exist. Reset it.
                await rate_limit_collection.update_one(
                    {"_id": sender_id},
                    {"$set": {"count": 1, "window_start": now}},
                    upsert=True
                )
                count = 1
                
            if count > rate_limit_max:
                logger.warning(f"Persistent circuit breaker tripped for {sender_id}. Dropping message.")
                return
        except Exception as e:
            # Rate limiter DB failure — always allow the message through.
            # Dropping messages on DB error is worse than allowing a rare loop through.
            logger.error(f"Rate limiter DB failure for {sender_id}: {e} — allowing message through")
        # ───────────────────────────────────────────────────────────────
        
        logger.info(f"📩 Processing message {message_id} from {sender_id}")
        await send_typing_indicator(sender_id, message_id)
        customer_id = await get_or_create_customer(sender_id)

        # ── Flow-response detection ───────────────────────────────────────────
        is_flow_response = "interactive" in message_payload and message_payload["interactive"].get("type") == "nfm_reply"

        # Keep guest_profile available for downstream handlers that need it.
        # We no longer block browsing for unregistered users — profile is built
        # lazily when they enter Order Services (villa_onboarding.py).
        guest_profile = await get_guest_context_by_phone(sender_id)

        # ── Order Services onboarding: mid-flow session continuation ──────────
        # Handles users who are partway through zone/villa/name/date collection.
        # Only fires when has_active_session() returns True — no new sessions here.
        if not is_flow_response and not _is_admin(sender_id):
            from app.utils.villa_onboarding import has_active_session, handle_if_needed as _villa_onboard
            if has_active_session(sender_id) and await _villa_onboard(sender_id, message_payload):
                return
        # ─────────────────────────────────────────────────────────────────────

        message_text = None
        serviceitems_text = None
        category_text = None
        selected_id = None

        if "text" in message_payload:
            message_text = message_payload["text"]["body"].strip()

            # ── Admin command intercept (REASSIGN / REFUND) ──────────────────────────
            # Checked before any other routing so admin commands always take priority.
            if _is_admin(sender_id) and message_text:
                if await _handle_admin_command(sender_id, message_text):
                    return

            # ── SP post-accept cancellation (CANCEL <order_number>) ───────────────────
            # Allows an SP who already accepted to cancel before payment, triggering
            # auto-reassignment to the next available provider.
            if message_text and message_text.upper().startswith("CANCEL "):
                if await _handle_sp_cancellation(sender_id, message_text):
                    return
            # ─────────────────────────────────────────────────────────────────────────

            # ── Auto-Responder Loop Prevention ───────────────────────────────────────
            if await _is_sender_sp(sender_id):
                logger.info(f"Intercepted text message from SP {sender_id}.")
                now = datetime.datetime.now(datetime.timezone.utc)
                warn_key = f"{sender_id}_sp_warn"
                
                from app.db.session import rate_limit_collection
                try:
                    # Update warning timestamp if 1 hour has passed
                    doc = await rate_limit_collection.find_one_and_update(
                        {
                            "_id": warn_key,
                            "$or": [
                                {"last_warning_sent_at": {"$lte": now - datetime.timedelta(hours=1)}},
                                {"last_warning_sent_at": {"$exists": False}}
                            ]
                        },
                        {"$set": {"last_warning_sent_at": now}},
                        return_document=True
                    )
                    
                    if not doc:
                        # Try to insert if it doesn't exist
                        try:
                            await rate_limit_collection.insert_one({"_id": warn_key, "last_warning_sent_at": now})
                            should_warn = True
                        except Exception:
                            should_warn = False
                    else:
                        should_warn = True
                        
                except Exception as e:
                    logger.error(f"Failed to check SP warning rate limit for {sender_id}: {e}")
                    should_warn = False
                
                if should_warn:
                    logger.info(f"Auto-replying to SP {sender_id} with instructions to prevent AI loop.")
                    await send_whatsapp_message(
                        sender_id, 
                        "🤖 *System Auto-Reply*\n\nPlease use the provided 'Accept' or 'Decline' buttons on the booking notification to respond. Free-text messages to this number are not monitored by human agents."
                    )
                else:
                    logger.info(f"SP {sender_id} free-text dropped silently due to 1-hour cooldown.")
                
                return
            # ─────────────────────────────────────────────────────────────────────────

        elif "audio" in message_payload:
            audio_id = message_payload["audio"]["id"]
            try:
                from app.utils.media_upload import download_whatsapp_media
                from app.services.openai_client import client as _oai_client
                import io as _io
                _vn_bytes, _vn_ct = await download_whatsapp_media(audio_id)
                _vn_file = _io.BytesIO(_vn_bytes)
                _vn_file.name = "voice_note.ogg"
                _vn_resp = await _oai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=_vn_file
                )
                message_text = _vn_resp.text
                logger.info(f"Voice note transcribed for {sender_id}: '{message_text[:80]}'")
            except Exception as _vn_err:
                logger.error(f"Voice note transcription failed for {sender_id}: {_vn_err}")
                message_text = None

            # ── Direct route: voice note in an active persistent mode session ─────
            # Must happen immediately after transcription, before session interceptors
            # (issue detection at line ~5354, escape words, etc.) can hijack the message.
            # Also handles transcription failure gracefully instead of silent nothing.
            _active_mode = persistent_mode_sessions.get(sender_id)
            if _active_mode and _active_mode in PERSISTENT_MODE_CHAT_TYPES:
                if message_text:
                    _vn_chat_type = PERSISTENT_MODE_CHAT_TYPES[_active_mode]
                    _vn_query = f"[VOICE] {message_text}" if _vn_chat_type == "voice-translator" else message_text
                    _vn_data = await _whatsapp_ai_chat(sender_id, _vn_query, _vn_chat_type)
                    if _vn_data:
                        await send_whatsapp_message(sender_id, _vn_data)
                else:
                    await send_whatsapp_message(
                        sender_id,
                        "Sorry, I couldn't transcribe your voice note. Please try typing your request."
                    )
                return
        elif "image" in message_payload or "document" in message_payload or "audio" in message_payload:
            # Handle media uploads for passport/document submission OR issues
            media_info = message_payload.get("image") or message_payload.get("document") or message_payload.get("audio")
            media_id = media_info.get("id")
            
            if media_id:
                # If guest is in an active session (issue or passport), we let the specific handler below it
                if sender_id in issue_reporting_sessions or sender_id in passport_sessions:
                    pass 
                else:
                    if "image" in message_payload or "document" in message_payload:
                        # Option B: check for a recent open issue from this guest (last 20 mins)
                        _cutoff = datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
                        _recent_issue = await issue_collection.find_one(
                            {"sender_id": sender_id, "status": "open", "timestamp": {"$gte": _cutoff}},
                            sort=[("timestamp", -1)]
                        )
                        if _recent_issue:
                            _vc = _recent_issue.get("villa_code") or await get_user_villa_code(sender_id) or "UNKNOWN"
                            _mtype = "image" if "image" in message_payload else "document"
                            _ok, _att_url, _ = await process_whatsapp_issue(
                                sender_id, media_id, _vc,
                                _recent_issue.get("description", "Issue follow-up"), _mtype,
                                customer_id=customer_id
                            )
                            if _ok:
                                await issue_collection.update_one(
                                    {"_id": _recent_issue["_id"]},
                                    {"$set": {"photo_url": _att_url}}
                                )
                                await send_whatsapp_message(sender_id, "📎 *Photo added to your report.*\n\nThank you! Our team has been notified.")
                            else:
                                await send_whatsapp_message(sender_id, "⚠️ Couldn't attach your photo. Please try again.")
                            return

                        # Option C: no recent issue — ask the guest what the image is for
                        pending_media_sessions[sender_id] = {
                            "media_id": media_id,
                            "media_type": "image" if "image" in message_payload else "document",
                            "timestamp": datetime.datetime.now()
                        }
                        _buttons_payload = {
                            "messaging_product": "whatsapp",
                            "to": sender_id,
                            "type": "interactive",
                            "interactive": {
                                "type": "button",
                                "body": {"text": "What is this image for? Please choose:"},
                                "action": {
                                    "buttons": [
                                        {"type": "reply", "reply": {"id": "pending_media_issue", "title": "Report an Issue"}},
                                        {"type": "reply", "reply": {"id": "pending_media_passport", "title": "Submit Passport"}}
                                    ]
                                }
                            }
                        }
                        import httpx as _httpx
                        _headers = {"Authorization": f"Bearer {settings.access_token}", "Content-Type": "application/json"}
                        async with _httpx.AsyncClient() as _c:
                            await _c.post(settings.whatsapp_api_url, json=_buttons_payload, headers=_headers)
                        return
                    else:
                        # Audio outside of session — pass to AI
                        pass
        elif "interactive" in message_payload:
            persistent_mode_sessions.pop(sender_id, None)
            # NOTE: language_lesson_sessions is NOT cleared here — button taps (Yes/No/Phrases)
            # must be able to read and update the session inside the button_reply handlers below.
            interactive_type = message_payload["interactive"].get("type")

            if interactive_type == "button_reply":
                category_text = message_payload["interactive"]["button_reply"]["title"]
                button_id = message_payload["interactive"]["button_reply"]["id"]

                # --- Existing button_reply logic ---
                if button_id.startswith("confirm_decline_"):
                    order_num = button_id.split("_", 2)[2]
                    await send_whatsapp_message(
                        sender_id,
                        "Thank you for confirming. We've noted your unavailability for this request.\n"
                        "_Terima kasih telah mengonfirmasi. Kami telah mencatat ketidaksediaan Anda untuk permintaan ini._"
                    )
                    # Add this SP to declined_by list and attempt reassignment
                    await order_collection.update_one(
                        {"order_number": order_num},
                        {"$addToSet": {"declined_by": sender_id}}
                    )
                    decline_sessions.pop(sender_id, None)
                    await attempt_sp_reassignment(order_num)
                    return

                if button_id.startswith("cancel_decline_"):
                    order_num = button_id.split("_", 2)[2]
                    order = await get_order_by_number(order_num)
                    if order and order.get("status") == "pending":
                        await send_whatsapp_order_to_SP(sender_id, order)
                        decline_sessions.pop(sender_id, None)
                    else:
                        await send_whatsapp_message(sender_id, "Order no longer available")
                    return

                # ── Pending-media routing (Option B+C follow-up) ────────────
                if button_id == "pending_media_issue":
                    pending = pending_media_sessions.pop(sender_id, None)
                    if not pending:
                        await send_whatsapp_message(sender_id, "Sorry, your image session expired. Please send the photo again.")
                        return
                    _vc = await get_user_villa_code(sender_id) or "UNKNOWN"
                    _ok, _att_url, _transcript = await process_whatsapp_issue(
                        sender_id, pending["media_id"], _vc,
                        f"📸 Issue photo submitted by Guest {sender_id[-4:]}", pending["media_type"],
                        customer_id=customer_id
                    )
                    if _ok:
                        _description = _transcript or f"📸 Issue photo submitted by Guest {sender_id[-4:]}"
                        _rich = await db["villa_profiles"].find_one({"villa_code": _vc})
                        _vinfo = await get_villa_info_by_code(_vc)
                        _mgr = (_rich or {}).get("manager_phone") or (_vinfo or {}).get("manager_number")
                        if _mgr:
                            _clean = "".join(filter(str.isdigit, str(_mgr)))
                            try:
                                await send_whatsapp_message(
                                    _clean,
                                    f"🚨 *NEW ISSUE REPORTED!*\n\nVilla: *{(_vinfo or {}).get('name', _vc)}*\n"
                                    f"Guest ID: `...{sender_id[-4:]}`\n"
                                    f"Issue: {_description}\n🖼️ *Attachment:* [View Media]({_att_url})\n\nPlease check the dashboard."
                                )
                            except Exception:
                                pass
                        await send_whatsapp_message(
                            sender_id,
                            "✅ *Issue Received*\n\nThank you for reporting this. Our maintenance team and the villa manager have been notified."
                        )
                    else:
                        await send_whatsapp_message(sender_id, "⚠️ Couldn't process your photo. Please try again.")
                    return

                if button_id == "pending_media_passport":
                    # PASSPORT-DIRECT-ATTACH-NAME-01 (2026-09-07, live report
                    # — Prakash: a guest resubmitting a passport by directly
                    # attaching a photo — e.g. right after a rejection —
                    # never had a name question at all, unlike the guided
                    # "Passport Submission" menu flow. Name resolution relied
                    # entirely on guest_profile_collection, which the guided
                    # flow's own "awaiting_name" step never writes back to —
                    # so this path silently fell back to
                    # f"WhatsApp Guest {sender_id[-4:]}" (media_upload.py) or
                    # whatever unrelated name happened to be in the guest's
                    # profile from registration/booking. Fixed by asking for
                    # the name here too, via the same passport_sessions
                    # mechanism the guided flow already uses — this is the
                    # highest-stakes moment to get identity right, since it's
                    # the exact resubmission a rejection prompts.
                    pending = pending_media_sessions.pop(sender_id, None)
                    if not pending:
                        await send_whatsapp_message(sender_id, "Sorry, your image session expired. Please send the photo again.")
                        return
                    # PASSPORT-DIRECT-ATTACH-NAME-01: guest_name is resolved
                    # here for guest_id purposes only -- the eventual upload's
                    # guest_name is never taken from it. The
                    # "awaiting_name_for_pending_media" step below always asks
                    # for (and requires a non-blank) fresh typed name, so the
                    # resolved profile name is intentionally not threaded onto
                    # the session -- there is no code path where it would ever
                    # be used, unlike the guided "awaiting_name" flow, which
                    # only asks once and needs a fallback for a blank answer.
                    _vc, _pending_guest_id, _pending_guest_name = await _resolve_villa_and_guest_for_passport(sender_id)
                    passport_sessions[sender_id] = {
                        "step": "awaiting_name_for_pending_media",
                        "villa_code": _vc,
                        "guest_id": _pending_guest_id,
                        "media_id": pending["media_id"],
                        "timestamp": datetime.datetime.now(),
                    }
                    await send_whatsapp_message(
                        sender_id,
                        "🛂 *Passport Submission*\n\nPlease enter your *Full Name* as it appears on your passport:"
                    )
                    return
                # ────────────────────────────────────────────────────────────

                # ── Booking form: time slot buttons ──────────────────────────────
                if button_id.startswith("bk_time_"):
                    bk_session = await _get_booking_session(sender_id)
                    if bk_session:
                        time_map = {
                            "bk_time_1": "12:00-14:00",
                            "bk_time_2": "14:00-16:00",
                            "bk_time_3": "16:00-18:00",
                        }
                        bk_session["time"] = time_map.get(button_id, "12:00-14:00")
                        bk_session["step"] = "awaiting_persons"
                        await _save_booking_session(sender_id, bk_session)
                        await _send_persons_list(sender_id)
                    return

                if button_id == "bk_confirm":
                    bk_session = await _delete_booking_session(sender_id)
                    if not bk_session:
                        await send_whatsapp_message(sender_id, "No active booking found. Please start again from the menu.")
                        return
                    service_name = bk_session.get("service_name", "")
                    date_str     = bk_session.get("date", "")
                    time_str     = bk_session.get("time", "12:00-14:00")
                    persons      = bk_session.get("persons", "1")
                    price_str    = bk_session.get("price", "")
                    cust_name    = bk_session.get("name", "")
                    cust_phone   = bk_session.get("phone", sender_id)
                    try:
                        import dateutil.parser as _dp
                        user_date = _dp.parse(date_str, dayfirst=True)
                    except Exception:
                        user_date = datetime.datetime.now()
                    # ── CRITICAL INVARIANT (bk_confirm path) ─────────────────
                    # BG-3, BG-4: resolve_customer_context() MUST be called here.
                    # Do NOT call get_user_villa_code() or read any session variable
                    # for villa_code. DB is the only authoritative source.
                    # Enforced by: tests/protected_flow/test_booking_context_gate.py
                    # ─────────────────────────────────────────────────────────────
                    from app.services.customer_context import resolve_customer_context as _rcc, CustomerContextError as _CCE
                    _ctx = await _rcc(
                        sender_id=sender_id,
                        phone_number=cust_phone if cust_phone != sender_id else None,
                        payload_villa_code=None,  # WhatsApp: no URL param, DB is only source
                    )
                    if isinstance(_ctx, _CCE):
                        # No villa context — the villa-code ask flow will handle this on next message
                        logger.warning("BOOKING", f"WA booking blocked: {_ctx.code} for {sender_id}")
                        await send_whatsapp_message(
                            sender_id,
                            "We couldn't determine your villa. Please send your Villa Code (e.g. V1) so we can complete your booking."
                        )
                        await _save_vc_session(sender_id, "pending")
                        return
                    _bk_villa_code = _ctx.villa_code
                    location_zone  = _ctx.location_zone or ""

                    base_price = await get_location_specific_price(service_name, _bk_villa_code)
                    new_order = await initiate_chat_session(
                        sender_id=sender_id,
                        service_name=service_name,
                        person_count=persons,
                        base_price=base_price,
                        date=user_date,
                        time=time_str,
                    )
                    new_order.date   = user_date
                    new_order.time   = time_str
                    new_order.status = "pending"
                    order_dict = new_order.dict()
                    order_dict["confirmation"]        = False
                    order_dict["customer_id"]         = customer_id
                    order_dict["customer_name"]       = cust_name
                    order_dict["phone_number"]        = cust_phone
                    order_dict["booking_date"]        = date_str
                    order_dict["persons"]             = persons
                    order_dict["villa_code"]           = _bk_villa_code
                    order_dict["location_zone"]        = location_zone
                    order_dict["villa_context_source"] = _ctx.source
                    from app.models.order_summary import PayoutStatus as _PS
                    order_dict["payout_status"]        = _PS.PENDING
                    await save_order_to_db(order_dict)
                    try:
                        price_cleaned = int(re.sub(r"[^\d]", "", str(new_order.price)) or "0")
                        num_persons = int(persons) if str(persons).isdigit() else 1
                        total_price = price_cleaned * num_persons
                        price_display = f"IDR {total_price:,}"
                    except Exception:
                        price_display = price_str or f"IDR {new_order.price}"
                    booking_summary_block = (
                        f"🎉 *Booking Request Received!*\n\n"
                        f"Here's your booking summary:\n"
                        f"──────────────────────\n"
                        f"📋 *Order ID:* `{new_order.order_number}`\n"
                        f"🧖 *Service:* {new_order.service_name}\n"
                        + (f"👤 *Name:* {cust_name}\n" if cust_name else "")
                        + f"📅 *Date:* {date_str}\n"
                        f"⏰ *Time:* {time_str}\n"
                        f"💰 *Total:* {price_display}\n"
                        f"──────────────────────\n\n"
                    )

                    # Resolve + attempt SP notification BEFORE telling the guest anything
                    # was confirmed — the guest must never be told "a provider has been
                    # notified" until that has actually been attempted and we know the
                    # outcome. See CLAUDE.md "SP Notification Structural Integrity".
                    service_numbers = await fetch_whatsapp_numbers(service_name, location_zone)
                    logger.info(f"🚀 Notifying {len(service_numbers)} SPs in {location_zone}: {service_numbers}")
                    _notified_numbers = []
                    for num in service_numbers:
                        try:
                            await send_whatsapp_order_to_SP(num, order_dict)
                            _notified_numbers.append(num)
                        except Exception as _sp_err:
                            logger.error(f"Failed to notify SP {num}: {_sp_err}")

                    await order_collection.update_one(
                        {"order_number": new_order.order_number},
                        {"$set": {
                            "sp_notified_at": datetime.datetime.now(),
                            "sp_notified_numbers": _notified_numbers,
                        }},
                    )

                    if _notified_numbers:
                        confirmation_message = booking_summary_block + (
                            f"⏳ A service provider has been notified and will confirm shortly.\n\n"
                            f"Once confirmed, your *secure payment link* will appear right here in this chat. Please keep it open! 🔔"
                        )
                    else:
                        confirmation_message = booking_summary_block + (
                            f"⏳ We're arranging a service provider for your booking — this may take a little longer than usual.\n\n"
                            f"We'll follow up here as soon as it's confirmed. 🔔"
                        )
                        # No SP could be reached at booking time — this must never sit
                        # silently in a log file. Alert admin so it gets manual follow-up.
                        try:
                            admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
                            await send_whatsapp_message(
                                admin_number,
                                f"🚨 *Order {new_order.order_number} — no SP notified*\n\n"
                                f"Service: *{service_name}*\n"
                                f"Villa: `{_bk_villa_code}`\n"
                                f"Guest: `{sender_id[-4:]}`\n\n"
                                f"No provider could be reached at booking time. Manual follow-up required."
                            )
                        except Exception as _admin_err:
                            logger.error(f"Failed to send no-SP admin alert for {new_order.order_number}: {_admin_err}")

                    await send_whatsapp_message(sender_id, confirmation_message)
                    return

                if button_id == "bk_cancel":
                    await _delete_booking_session(sender_id)
                    await send_whatsapp_message(sender_id, "Booking cancelled. Type *menu* to start over.")
                    return
                # ─────────────────────────────────────────────────────────────────

                # ── Follow-up button tapped ───────────────────────────────────────
                if button_id.startswith("fup_"):
                    fu_data = followup_sessions.get(sender_id, {})
                    fu_item = fu_data.get(button_id)
                    if fu_item:
                        query = fu_item.get("query", "")
                        if query == "__THIS_WEEK_EVENTS__":
                            import datetime as _dt_mod
                            _today = _dt_mod.datetime.now()
                            _wstart = _today - _dt_mod.timedelta(days=_today.weekday())
                            _wend = _wstart + _dt_mod.timedelta(days=6)
                            _dr = f"{_wstart.strftime('%d %B %Y')} to {_wend.strftime('%d %B %Y')}"
                            query = f"What events are happening this week ({_dr}) in Bali?"
                        key = fu_item.get("key", "followup_general")
                        chat_type = PERSISTENT_MODE_CHAT_TYPES.get(key, "general")
                        persistent_mode_sessions[sender_id] = key
                        data = await _whatsapp_ai_chat(sender_id, query, chat_type)
                        if data:
                            await send_whatsapp_message(sender_id, data)
                    return

                # ── Button-based sheet navigation (Recommendations) ──────────────
                if button_id.startswith("shbcat_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    id_map = nav.get("id_map", {})
                    cat_name = id_map.get(button_id, category_text)
                    main_menu = nav.get("main_menu", "Recommendations")
                    subs = await get_sheet_menu_subcategories(main_menu, cat_name)
                    if subs and len(subs) <= 3:
                        sub_id_map = {}
                        btns = []
                        for i, s in enumerate(subs):
                            sid = f"shbsub_{i}"
                            sub_id_map[sid] = s["subcategory"]
                            btns.append({"id": sid, "title": s["subcategory"][:20]})
                        sheet_nav_sessions[sender_id] = {
                            "main_menu": main_menu,
                            "category": cat_name,
                            "id_map": sub_id_map,
                            "use_buttons": True,
                        }
                        await _send_nav_buttons(sender_id, f"*{cat_name}*\nChoose an option:", btns)
                    elif subs and len(subs) > 3:
                        # Fallback to list if more than 3 subcategories
                        sub_id_map = {}
                        rows = []
                        for i, s in enumerate(subs):
                            sid = f"shsub_{i}"
                            sub_id_map[sid] = s["subcategory"]
                            rows.append({"id": sid, "title": s["subcategory"][:24], "description": "Tap to explore"})
                        sheet_nav_sessions[sender_id] = {"main_menu": main_menu, "category": cat_name, "id_map": sub_id_map}
                        await send_whatsapp_menu_list_message(sender_id, {"main_title": cat_name, "main_description": "Choose an option:", "data": rows})
                    else:
                        # No subcategories — execute endpoint directly
                        endpoint = await get_sheet_menu_endpoint(main_menu, cat_name)
                        await _execute_sheet_endpoint(sender_id, endpoint, cat_name, main_menu)
                    return

                if button_id.startswith("shbsub_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    id_map = nav.get("id_map", {})
                    sub_name = id_map.get(button_id, category_text)
                    main_menu = nav.get("main_menu", "Recommendations")
                    category = nav.get("category", "")
                    # Check for 3rd navigation level before executing endpoint
                    subsubs = await get_sheet_menu_sub_subcategories(main_menu, category, sub_name)
                    if subsubs:
                        sub2_id_map = {}
                        if len(subsubs) <= 3:
                            btns = []
                            for i, s in enumerate(subsubs):
                                sid = f"shbsub2_{i}"
                                sub2_id_map[sid] = s["sub_subcategory"]
                                btns.append({"id": sid, "title": s["sub_subcategory"][:20]})
                            sheet_nav_sessions[sender_id] = {
                                "main_menu": main_menu,
                                "category": category,
                                "subcategory": sub_name,
                                "id_map": sub2_id_map,
                            }
                            await _send_nav_buttons(sender_id, f"*{sub_name}*\nChoose an option:", btns)
                        else:
                            rows = []
                            for i, s in enumerate(subsubs):
                                sid = f"shsub2_{i}"
                                sub2_id_map[sid] = s["sub_subcategory"]
                                rows.append({"id": sid, "title": s["sub_subcategory"][:24], "description": "Tap to explore"})
                            sheet_nav_sessions[sender_id] = {
                                "main_menu": main_menu, "category": category,
                                "subcategory": sub_name, "id_map": sub2_id_map,
                            }
                            await send_whatsapp_menu_list_message(sender_id, {
                                "main_title": sub_name, "main_description": "Choose an option:", "data": rows,
                            })
                    else:
                        endpoint = await get_sheet_menu_endpoint(main_menu, category, sub_name)
                        await _execute_sheet_endpoint(sender_id, endpoint, sub_name, main_menu)
                    return

                if button_id.startswith("shbsub2_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    id_map = nav.get("id_map", {})
                    subsub_name = id_map.get(button_id, category_text)
                    main_menu = nav.get("main_menu", "Recommendations")
                    category = nav.get("category", "")
                    subcategory = nav.get("subcategory", "")
                    endpoint = await get_sheet_menu_endpoint(main_menu, category, subcategory, subsub_name)
                    await _execute_sheet_endpoint(sender_id, endpoint, subsub_name, main_menu)
                    return
                # ─────────────────────────────────────────────────────────────────

                if button_id == "language_yes":
                    session = language_lesson_sessions.get(sender_id, {})
                    word_index = session.get("word_index", 0) + 1 if isinstance(session, dict) else 1
                    language_lesson_sessions[sender_id] = {
                        "mode": session.get("mode", "structured") if isinstance(session, dict) else "structured",
                        "word_index": word_index,
                        "timestamp": datetime.datetime.now(),
                    }
                    await language_yes_message(sender_id, word_index)
                    return
                if button_id == "language_no":
                    await language_no_message(sender_id)
                    return
                if button_id == "language_phrase":
                    # Switch to freestyle mode — user will type phrases next
                    session = language_lesson_sessions.get(sender_id, {})
                    language_lesson_sessions[sender_id] = {
                        "mode": "freestyle",
                        "word_index": session.get("word_index", 0) if isinstance(session, dict) else 0,
                        "timestamp": datetime.datetime.now(),
                    }
                    await send_whatsapp_message(
                        sender_id,
                        "Sure! Feel free to ask us about any word or phrase you're curious about – "
                        "we're happy to help! 😊\n\n"
                        "For example: 'How do I say thank you in Balinese?' or 'What does selamat pagi mean?'"
                    )
                    return
                if button_id == "back_to_menu":
                    language_lesson_sessions.pop(sender_id, None)
                    await starting_message(sender_id)
                    return

                if button_id == "menu_button":
                    api_url = f"{settings.BASE_URL}/main_design"
                    menu_data = await fetch_menu_data(api_url, "Main Menu")
                    if menu_data:
                        print(f"🔍 DEBUG - menu_data type: {type(menu_data)}")
                        print(f"🔍 DEBUG - menu_data content: {menu_data}")
                        if isinstance(menu_data, list):
                            menu_data = {"data": menu_data}
                        await send_whatsapp_menu_list_message(recipient_id=sender_id, card_data=menu_data)
                    return

                if button_id == "wa_ami_confirm":
                    session = amenity_wa_sessions.get(sender_id)
                    if not session or not session.get("selected_item"):
                        await send_whatsapp_message(sender_id, "⏱️ Session expired. Please start again from the menu.")
                        return
                    item = session["selected_item"]
                    villa_code = session.get("villa_code", "")
                    try:
                        _now = datetime.datetime.utcnow()
                        villa_name = villa_code
                        try:
                            _vi = await get_villa_info_by_code(villa_code)
                            if _vi:
                                villa_name = _vi.get("name") or villa_code
                        except Exception:
                            pass
                        from app.db.session import db as _amenity_db
                        await _amenity_db["amenities"].insert_one({
                            "sender_id": sender_id,
                            "guest_id": sender_id,
                            "guest_phone": sender_id,
                            "villa_code": villa_code,
                            "villa_name": villa_name,
                            "request_description": item,
                            "item_type": item,
                            "quantity": 1,
                            "source": "whatsapp",
                            "urgency": "normal",
                            "status": "open",
                            "history": [{"status": "open", "timestamp": _now, "note": "Request received via WhatsApp"}],
                            "created_at": _now,
                            "updated_at": _now,
                        })
                        try:
                            villa_phone = await get_villa_whatsapp_by_code(villa_code)
                            if villa_phone:
                                _ts = _now.strftime("%d %b %Y, %H:%M UTC")
                                await send_whatsapp_message(
                                    villa_phone,
                                    f"🛎️ *New Amenity Request*\n\n"
                                    f"• *Villa:* {villa_name}\n"
                                    f"• *Item:* {item}\n"
                                    f"• *Time:* {_ts}\n"
                                    f"• *Source:* WhatsApp\n"
                                    f"• *Status:* Open\n\n"
                                    f"Please fulfil this request and update in the Host Interface."
                                )
                        except Exception as _ne:
                            logger.warning(f"Amenity WA staff notification failed: {_ne}")
                        amenity_wa_sessions.pop(sender_id, None)
                        await send_whatsapp_message(
                            sender_id,
                            f"✅ *Request Confirmed!*\n\n"
                            f"Your *{item}* request has been sent to villa staff. It will be with you shortly. 🛎️\n\n"
                            f"_Is there anything else I can help with?_"
                        )
                        await starting_message(sender_id)
                    except Exception as _ae:
                        logger.error(f"Amenity WA submit failed: {_ae}")
                        await send_whatsapp_message(sender_id, "Sorry, we couldn't submit your request. Please try again.")
                    return

                if button_id == "wa_ami_change":
                    session = amenity_wa_sessions.get(sender_id)
                    if session:
                        session["step"] = "awaiting_item"
                        session["timestamp"] = datetime.datetime.now()
                        amenity_wa_sessions[sender_id] = session
                        await _send_amenity_items_list(sender_id)
                    else:
                        await starting_message(sender_id)
                    return

                if button_id in ("issue_button", "wa_report_issue"):
                    issue_reporting_sessions[sender_id] = {"step": "awaiting_description", "timestamp": datetime.datetime.now()}
                    await send_whatsapp_message(
                        sender_id,
                        "⚠️ *Issue Reporting Mode*\n\n"
                        "I'm sorry to hear you're experiencing an issue. Please describe the problem in detail.\n\n"
                        "You can also send a **Photo** or **Voice Note** to help us understand the situation better. 📸 🎤\n\n"
                        "_Type *CANCEL* to exit issue reporting._"
                    )
                    return

                if button_id == "wa_request_amenities":
                    _villa_code = await get_user_villa_code(sender_id)
                    if not _villa_code:
                        await send_whatsapp_message(
                            sender_id,
                            "🏡 *Villa Not Detected*\n\n"
                            "Please scan your villa QR code first to link your stay, then try requesting amenities."
                        )
                        return
                    amenity_wa_sessions[sender_id] = {
                        "step": "awaiting_item",
                        "villa_code": _villa_code,
                        "timestamp": datetime.datetime.now(),
                    }
                    await _send_amenity_items_list(sender_id)
                    return

                if button_id == "chat_button":
                    await send_whatsapp_message(
                        sender_id,
                        "💬 You can now chat with us! Just send your message and we'll be happy to help you with anything you need during your stay in Bali."
                    )
                    # Task 20: Flag support activation
                    asyncio.create_task(log_guest_inquiry(
                        sender_id,
                        user_villa_code or "WEB_VILLA_01",
                        "CLICKED: Chat with Us",
                        "Assigned to Support",
                        intent="support_request"
                    ))
                    return

                if category_text in ["✅ Accept", "❌ Decline"]:
                    if category_text == "✅ Accept":
                        order_num = message_payload["interactive"]["button_reply"]["id"]
                        # Check order status BEFORE processing — reject timed-out or cancelled orders
                        _ia_order_doc = await order_collection.find_one({"order_number": order_num})
                        if _ia_order_doc:
                            _ia_bk_status = _ia_order_doc.get("booking_status", "")
                            _ia_legacy = _ia_order_doc.get("status", "")
                            _ia_closed = _ia_bk_status in ("FAILED", "CANCELLED", "COMPLETED") or _ia_legacy in ("sp_timeout", "cancelled", "CANCELLED", "completed")
                            if _ia_closed:
                                await send_whatsapp_message(
                                    sender_id,
                                    f"⏰ *This booking has expired.*\n\n"
                                    f"Order *{order_num}* was cancelled because no service provider "
                                    f"accepted within the required time. The guest has been notified.\n\n"
                                    f"Thank you for your response!"
                                )
                                return
                        service_provider_code = await get_service_provider_by_whatsapp(sender_id)
                        await order_collection.update_one(
                            {"order_number": order_num},
                            {"$set": {"service_provider_code": service_provider_code}}
                        )
                        local_order_store[sender_id] = order_num
                        session_id = await get_sender_id_by_order(order_num)

                        if session_id in website_sessions:
                            confirmation = await check_order_confirmation(order_num)
                            if not confirmation:
                                await send_confirmation_order_to_SP(sender_id, order_num)
                            else:
                                await send_whatsapp_message(sender_id, "Thank you for the acceptance. Unfortunately, this order has already been booked.")
                        else:
                            order_num = message_payload["interactive"]["button_reply"]["id"]
                            order_sessions[sender_id] = order_num
                            confirmation = await check_order_confirmation(order_num)
                            if not confirmation:
                                await send_confirmation_order_to_SP(sender_id, order_num)
                            else:
                                await send_whatsapp_message(sender_id, "Thank you for the acceptance. Unfortunately, this order has already been booked.")
                    elif category_text == "❌ Decline":
                        if button_id.startswith("decline_"):
                            order_num = button_id.split("_", 1)[1]
                            decline_sessions[sender_id] = order_num
                            await send_decline_confirmation(sender_id, order_num)
                        else:
                            await send_whatsapp_message(sender_id, "Invalid decline request.")
                        return

                elif category_text == "Yes":
                    logger.info("order number received")
                    if button_id.startswith("yes_order_"):
                        order_num = button_id.split("_", 2)[2]
                    else:
                        order_num = local_order_store.get(sender_id)
                    
                    logger.info(f"Order number for confirmation: {order_num}")
                    if order_num:
                        service_provider_code = await get_service_provider_by_whatsapp(sender_id)
                        
                        # [BUGFIX]: Atomic check to prevent race conditions. Only update if NOT already confirmed.
                        updated_order = await order_collection.find_one_and_update(
                            {
                                "order_number": order_num,
                                "confirmed_by_provider": None
                            },
                            {
                                "$set": {
                                    "confirmed_by_provider": sender_id,
                                    "confirmed_at": datetime.datetime.now(),
                                    "service_provider_code": service_provider_code
                                }
                            },
                            return_document=True
                        )

                        if not updated_order:
                            # It was already claimed or doesn't exist.
                            already_claimed = await order_collection.find_one({"order_number": order_num})
                            if already_claimed and already_claimed.get("confirmed_by_provider"):
                                await send_whatsapp_message(
                                    sender_id, 
                                    "⚠️ *Oops! Too late.*\n\nThis booking has already been claimed by another service provider. Thank you for your swift response, better luck next time!"
                                )
                            else:
                                await send_whatsapp_message(sender_id, "Order not found or an error occurred.")
                            
                            order_sessions.pop(sender_id, None)
                            return
                        user_sender_id = await update_order_confirmation(order_num, True)
                        logger.info(f"Order {order_num} confirmed by SP. Customer sender_id: {user_sender_id}")

                        # Transition booking_status: AWAITING_SP_CONFIRMATION → AWAITING_PAYMENT
                        # This is required so the xendit webhook can later transition AWAITING_PAYMENT → CONFIRMED.
                        # Without this step the webhook transition is blocked by the FSM and booking_status
                        # stays stuck at AWAITING_SP_CONFIRMATION, preventing disbursement.
                        from app.services.status_service import BookingStatusManager
                        from app.models.order_summary import BookingStatus as _BS
                        sp_transition = await BookingStatusManager.transition_booking_status(
                            order_num,
                            _BS.AWAITING_PAYMENT,
                            f"SP_{sender_id}",
                            reason=f"SP {sender_id} accepted the booking — payment link being generated"
                        )
                        if not sp_transition.get("success"):
                            logger.warning(f"booking_status FSM transition to AWAITING_PAYMENT failed for {order_num}: {sp_transition.get('error')}")

                        order_data = await order_collection.find_one({"order_number": order_num})
                        if not order_data:
                            await send_whatsapp_message(sender_id, "Order not found.")
                            return
                        try:
                            # Remove MongoDB _id field before instantiating Pydantic model
                            order_doc = {k: v for k, v in order_data.items() if k != "_id"}
                            order = Order(**order_doc)
                        except Exception as e:
                            logger.error(f"Error creating Order model for {order_num}: {e}")
                            await send_whatsapp_message(sender_id, "Error processing order. Please contact support.")
                            return
                        payment_result = await create_xendit_payment_with_distribution(order)
                        logger.info(f"Payment result for {order_num}: {payment_result}")

                        # GNAF-01 (2026-08-25): tracks whether the guest was ACTUALLY
                        # notified, so the SP-facing confirmation below never claims
                        # "the guest has been notified" when that never happened. Root
                        # cause of "SP accepted, guest got nothing": every failure
                        # branch here (no payment_url despite success=True, a websocket
                        # send with no guest connected) previously only logged an error
                        # server-side — no guest-facing fallback, no admin alert, and
                        # the SP was unconditionally told the guest was already notified,
                        # which masked the failure from ever being reported.
                        _guest_notified_ok = False

                        if payment_result.get('success'):
                            await update_order_with_payment_info(order_num, payment_result)
                            payment_url = payment_result.get('payment_url', '')

                            # Notify customer that their service request has been accepted
                            # Guard: user_sender_id could be None if order was created externally
                            if user_sender_id and str(user_sender_id).isdigit():
                                if payment_url:
                                    await send_booking_accepted_to_guest(
                                        user_sender_id,
                                        order_num,
                                        order_data.get('service_name', 'Your Service'),
                                        payment_url,
                                    )
                                    _guest_notified_ok = True
                                else:
                                    logger.error(f"No payment_url in result for order {order_num}")
                            elif user_sender_id:
                                payment_message = (
                                    "🌴 ***Your Order Awaits!***\nThank you for choosing GINI Bali.\n"
                                    "Please confirm your **order** by completing the payment through the secure link below.\n"
                                    "Once payment is confirmed, we'll take care of the rest — just sit back, relax, and your service will come to you as scheduled."
                                )
                                if payment_url:
                                    await manager.send_personal_message(
                                        message=f"{payment_message}\n[link]({payment_url})",
                                        session_id=user_sender_id,
                                        message_type="link_message"
                                    )
                                    _guest_notified_ok = True
                                else:
                                    logger.error(f"No payment_url in result for order {order_num}")

                            # GNAF-01: payment_result.success=True with no payment_url is
                            # rare (Xendit's own response object should always carry one),
                            # but when it happens it must never sit silently in a log file
                            # — the guest is left with nothing and no one is alerted.
                            if not _guest_notified_ok:
                                try:
                                    admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
                                    await send_whatsapp_message(
                                        admin_number,
                                        f"🚨 *Order {order_num} — guest NOT notified after SP accepted*\n\n"
                                        f"SP: `{sender_id[-4:]}`\n"
                                        f"Payment created (success=True) but no payment_url was returned "
                                        f"or the guest channel could not be reached. Manual follow-up required."
                                    )
                                except Exception as _gnaf_admin_err:
                                    logger.error(f"GNAF-01 admin alert failed for {order_num}: {_gnaf_admin_err}")
                        else:
                            error_detail = payment_result.get('error', 'Unknown Error')
                            error_message = (
                                f"⚠️ *Payment System Issue*\n\n"
                                f"Sorry, we encountered an issue creating your secure payment link:\n_{error_detail}_\n\n"
                                f"Please try again or contact support."
                            )
                            if user_sender_id and str(user_sender_id).isdigit():
                                await send_whatsapp_message(user_sender_id, error_message)
                            elif user_sender_id:
                                await manager.send_personal_message(message=error_message, session_id=user_sender_id, message_type="error")

                        # GNAF-01: the SP-facing confirmation must reflect what
                        # actually happened for the guest — never claim "notified"
                        # unconditionally (this previously masked every failure above
                        # from ever surfacing, since the SP always saw a clean success
                        # message regardless of outcome).
                        if _guest_notified_ok:
                            _sp_confirm_msg = (
                                "✅ You've successfully confirmed the booking! The guest has been notified and is completing payment. "
                                "You'll receive final details once the payment is confirmed.\n\n"
                                "_Anda telah berhasil mengonfirmasi pemesanan! Tamu telah diberitahu dan sedang menyelesaikan pembayaran. "
                                "Anda akan menerima detail akhir setelah pembayaran dikonfirmasi._"
                            )
                        else:
                            _sp_confirm_msg = (
                                "✅ You've successfully confirmed the booking. We're finalizing the guest's payment link — "
                                "our team has been alerted to follow up. You'll receive final details once payment is confirmed.\n\n"
                                "_Anda telah berhasil mengonfirmasi pemesanan. Kami sedang menyelesaikan tautan pembayaran tamu — "
                                "tim kami telah diberitahu untuk menindaklanjuti._"
                            )
                        await send_whatsapp_message(sender_id, _sp_confirm_msg)

                        # Closure notification to all other originally notified SPs
                        try:
                            _order_after = await order_collection.find_one({"order_number": order_num})
                            _all_notified = _order_after.get("sp_notified_numbers", []) if _order_after else []
                            _others = [n for n in _all_notified if n != sender_id]
                            logger.info(
                                f"Closure: order {order_num} accepted by {sender_id}. "
                                f"Notifying {len(_others)} other SP(s): {_others}"
                            )
                            if _others:
                                _closure_msg = (
                                    f"📋 *Booking Update — Order #{order_num}*\n\n"
                                    f"This booking has already been accepted by another service provider.\n\n"
                                    f"Thank you for your quick response! We hope to work with you on the next booking."
                                )
                                for _other_sp in _others:
                                    try:
                                        await send_whatsapp_with_fallback(
                                            recipient=_other_sp,
                                            freeform_msg=_closure_msg,
                                            template_name="booking_cancelled_sp",
                                            template_vars=[order_num],
                                            notification_type="booking_closed_sp",
                                            order_number=order_num,
                                        )
                                        logger.info(f"Closure notification sent to SP {_other_sp} for order {order_num}")
                                    except Exception as _close_err:
                                        logger.error(
                                            f"Failed closure notification to SP {_other_sp} for order {order_num}: {_close_err}"
                                        )
                        except Exception as _closure_block_err:
                            logger.error(f"Closure notification block failed for order {order_num}: {_closure_block_err}")

                        order_sessions.pop(sender_id, None)
                    else:
                        await send_whatsapp_message(sender_id, "No order found for confirmation.")

                elif category_text in ["No", "No, Go Back"]:
                    order_num = local_order_store.get(sender_id)
                    if order_num:
                        order = await get_order_by_number(order_num)
                        if order and order.get("status") == "pending":
                            await send_whatsapp_order_to_SP(sender_id, order)
                        else:
                            await send_whatsapp_message(sender_id, "This request is no longer available.")
                    else:
                        await send_whatsapp_message(sender_id, "No active request found.")
                    order_sessions.pop(sender_id, None)

                category_id = message_payload["interactive"]["button_reply"]["id"]
                service_name = category_id.replace("_", " ")

            elif interactive_type == "list_reply":
                list_reply = message_payload["interactive"]["list_reply"]
                serviceitems_text = list_reply["title"]
                selected_id = list_reply["id"]

                # ── What To Do Today? mood selection ─────────────────────────────
                if selected_id.startswith("wtd_"):
                    _wtd_labels = {
                        "wtd_instagram":    "I want nice photos for my Instagram",
                        "wtd_quality_time": "I want to spend quality time with my friends or family",
                        "wtd_local":        "I want to try a local experience",
                        "wtd_crazy":        "I want to do something crazy",
                    }
                    _mood = _wtd_labels.get(selected_id, serviceitems_text)
                    persistent_mode_sessions[sender_id] = "what_to_do_today"
                    _kickoff = (
                        f"The guest selected: '{_mood}'. "
                        f"Acknowledge their mood warmly in ONE sentence, then ask ONE smart follow-up question "
                        f"(e.g. are they solo/couple/family, how much time do they have, which area of Bali). "
                        f"Do NOT list activities yet. Keep it short and friendly."
                    )
                    _resp = await _whatsapp_ai_chat(sender_id, _kickoff, "what-to-do")
                    if _resp:
                        await send_whatsapp_message(sender_id, _resp)
                    return

                # ── Booking form: persons list reply ─────────────────────────────
                if selected_id.startswith("bk_persons_"):
                    bk_session = await _get_booking_session(sender_id)
                    if bk_session:
                        persons_map = {
                            "bk_persons_1": "1",
                            "bk_persons_2": "2",
                            "bk_persons_3": "3",
                            "bk_persons_4": "4",
                        }
                        bk_session["persons"] = persons_map.get(selected_id, "1")
                        bk_session["step"] = "awaiting_confirm"
                        await _save_booking_session(sender_id, bk_session)
                        await _send_booking_summary(sender_id)
                    return
                # ─────────────────────────────────────────────────────────────

                # ── WCR-13: AI catalog item tapped → Category Flow for booking ─
                # ── WCR-14: AI catalog/service item tapped → booking form for that service ─
                # When guest taps a specific service from the WCR-13 list, go directly to
                # the booking form (not Category Flow). Extracts service name from row ID,
                # looks up price from Google Sheets, calls _start_booking_flow().
                if selected_id.startswith("ai_catalog_") or selected_id.startswith("ai_service_"):
                    # Extract service name: ai_catalog_0_Single_or_Mix_Flavor → Single or Mix Flavor
                    try:
                        _aic_parts = selected_id.split("_", 3)
                        _aic_svc_name = _aic_parts[3].replace("_", " ") if len(_aic_parts) > 3 else selected_id
                    except Exception:
                        _aic_svc_name = selected_id
                    # Look up price from Google Sheets by matching service name
                    _aic_price = ""
                    try:
                        from app.services.google_sheets_service import google_sheets_service as _aic_gss
                        from app.utils.formatters import clean_price_string as _aic_cps
                        _aic_svcs = await _aic_gss.get_services_data()
                        _aic_match = next(
                            (s for s in (_aic_svcs or [])
                             if s.get("service_name", "").lower() == _aic_svc_name.lower()),
                            None
                        )
                        if _aic_match:
                            _aic_price = _aic_cps(_aic_match.get("price", ""))
                    except Exception as _aic_price_err:
                        logger.warning(f"[WCR-14] price lookup non-fatal: {_aic_price_err}")
                    # Villa-code JIT gate — ask first if unknown, same as Order Services tap
                    if await _gate_booking_on_villa_code(sender_id, _aic_svc_name, _aic_price):
                        return
                    # Start booking: WhatsApp Flow form (or web link fallback)
                    try:
                        await _start_booking_flow(sender_id, _aic_svc_name, _aic_price)
                    except Exception as _aic_bk_err:
                        logger.error(f"[WCR-14] booking flow failed: {_aic_bk_err}")
                    return

                # ── Order Services: check villa code, ask if unknown ──────────
                if selected_id == "order_services":
                    _os_vc = await get_user_villa_code(sender_id)
                    if not _os_vc:
                        await _save_vc_session(sender_id, "order_services")
                        await send_whatsapp_message(
                            sender_id,
                            "To show you the right services and prices, we need to know your villa.\n\n"
                            "Do you have your *villa code*? It's a short code like *V1* or *V2* — "
                            "you'll find it on your welcome card or villa QR sticker.\n\n"
                            "• *Type your villa code* — e.g. V1\n"
                            "• Reply *no* if you don't have it and we'll help you find your villa"
                        )
                        return
                    _cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                    if _cat_flow_id:
                        import uuid as _uuid_os2
                        _os2_token = f"cat_{sender_id}_{_uuid_os2.uuid4().hex[:8]}"
                        from app.services.whatsapp_flows_service import send_category_flow_message as _send_os2
                        try:
                            await _send_os2(sender_id, _cat_flow_id, _os2_token)
                        except Exception as _os2_err:
                            logger.error(f"Category flow send failed for {sender_id}: {_os2_err}")
                    return

                # 0. Handle Passport Submission Start
                if selected_id == "passport_submission":
                    _villa_code = await get_user_villa_code(sender_id)
                    if not _villa_code:
                        await send_whatsapp_message(
                            sender_id,
                            "🏡 *Villa Not Detected*\n\n"
                            "Passport submission is available to registered villa guests only.\n\n"
                            "Please scan your villa QR code to get started, then select Passport Submission again."
                        )
                        return
                    passport_sessions[sender_id] = {"step": "awaiting_name", "villa_code": _villa_code, "timestamp": datetime.datetime.now()}
                    await send_whatsapp_message(
                        sender_id,
                        "🛂 *Passport Submission*\n\nPlease enter your *Full Name* as it appears on your passport:"
                    )
                    return

                # ── Discounts & Promotions → WhatsApp Flow (fallback to list) ──
                elif selected_id == "discount__promotions":
                    _dnp_sent = False
                    try:
                        from app.services.whatsapp_flows_service import send_dnp_flow_message as _send_dnp_flow
                        await _send_dnp_flow(sender_id, f"dnp_{sender_id[:20]}")
                        _dnp_sent = True
                    except Exception as _dnp_err:
                        logger.warning(f"DNP flow send failed (falling back to list): {_dnp_err}")
                    if not _dnp_sent:
                        await _send_dnp_categories(sender_id)
                    return

                # 1. Handle Categorized Selection (from Main Menu)
                    # Fetch subcategories for this category
                    category_title = serviceitems_text
                    api_url = f"{settings.BASE_URL}/categories/sections"
                    try:
                        async with httpx.AsyncClient() as client:
                            response = await client.post(api_url, json={"category_title": category_title})
                            if response.status_code == 200:
                                subcat_data = response.json().get("subcategories", [])
                                await send_whatsapp_subcategory_list_message(sender_id, subcat_data, category_title)
                                return
                    except Exception as e:
                        print(f"Error fetching subcategories: {e}")

                # ── Report Issue or Amenities → go directly to amenity items list ──
                elif selected_id == "report_issue_or_amenities":
                    _villa_code = await get_user_villa_code(sender_id)
                    if not _villa_code:
                        await send_whatsapp_message(
                            sender_id,
                            "🏡 *Villa Not Detected*\n\n"
                            "Please scan your villa QR code first to link your stay, then try again."
                        )
                        return
                    amenity_wa_sessions[sender_id] = {
                        "step": "awaiting_item",
                        "villa_code": _villa_code,
                        "timestamp": datetime.datetime.now(),
                    }
                    await _send_amenity_items_list(sender_id)
                    return

                # ── Request For Amenities (legacy — kept for old conversations) ──
                elif selected_id == "request_for_amenities":
                    villa_code = await get_user_villa_code(sender_id)
                    if not villa_code:
                        await send_whatsapp_message(
                            sender_id,
                            "🏡 *Villa Not Detected*\n\n"
                            "Please scan your villa QR code first to link your stay, then try requesting amenities."
                        )
                        return
                    amenity_wa_sessions[sender_id] = {
                        "step": "awaiting_item",
                        "villa_code": villa_code,
                        "timestamp": datetime.datetime.now(),
                    }
                    await _send_amenity_items_list(sender_id)
                    return

                # ── Report Maintenance (legacy — kept for old conversations) ──
                elif selected_id == "report_maintenance":
                    _villa_code = await get_user_villa_code(sender_id)
                    if not _villa_code:
                        await send_whatsapp_message(
                            sender_id,
                            "🏡 *Villa Not Detected*\n\n"
                            "Please scan your villa QR code first to link your stay, then try again."
                        )
                        return
                    issue_reporting_sessions[sender_id] = {
                        "step": "awaiting_description",
                        "villa_code": _villa_code,
                        "timestamp": datetime.datetime.now(),
                    }
                    await send_whatsapp_message(
                        sender_id,
                        "⚠️ *Maintenance Issue Reporting*\n\n"
                        "Please describe the problem in detail.\n\n"
                        "You can also send a *Photo* 📸 or *Voice Note* 🎤 to help explain the issue.\n\n"
                        "_Type *CANCEL* to exit._"
                    )
                    return

                # ── More Items (page 2 of amenity list) ──────────────────────
                elif selected_id == "wa_ami_more":
                    session = amenity_wa_sessions.get(sender_id)
                    if session:
                        session["timestamp"] = datetime.datetime.now()
                        amenity_wa_sessions[sender_id] = session
                        await _send_amenity_items_list(sender_id, page=2)
                    else:
                        await starting_message(sender_id)
                    return

                # ── Report Issue from amenity list (page 2 bottom row) ────────
                elif selected_id == "wa_issue_from_amenity":
                    amenity_wa_sessions.pop(sender_id, None)
                    issue_reporting_sessions[sender_id] = {
                        "step": "awaiting_description",
                        "timestamp": datetime.datetime.now(),
                    }
                    await send_whatsapp_message(
                        sender_id,
                        "⚠️ *Issue Reporting Mode*\n\n"
                        "I'm sorry to hear you're experiencing an issue. Please describe the problem in detail.\n\n"
                        "You can also send a *Photo* 📸 or *Voice Note* 🎤 to help explain the issue.\n\n"
                        "_Type *CANCEL* to exit._"
                    )
                    return

                # ── Amenity item selected from items list ─────────────────────
                elif selected_id.startswith("wa_ami_"):
                    session = amenity_wa_sessions.get(sender_id)
                    if not session:
                        await send_whatsapp_message(
                            sender_id,
                            "⏱️ Your session expired. Please select amenities from the menu again."
                        )
                        return
                    item_name = _AMENITY_ID_TO_ITEM.get(selected_id, serviceitems_text)
                    session["selected_item"] = item_name
                    session["step"] = "awaiting_confirm"
                    session["timestamp"] = datetime.datetime.now()
                    amenity_wa_sessions[sender_id] = session
                    await _send_amenity_confirm_buttons(sender_id, item_name)
                    return

                # ── DNP: category selected → show subcategory list ────────────
                elif selected_id.startswith("dnp_cat_"):
                    session = dnp_wa_sessions.get(sender_id, {})
                    cat_name = session.get("id_map", {}).get(selected_id, serviceitems_text)
                    await _send_dnp_subcategories(sender_id, cat_name)
                    return

                # ── DNP: promo selected → show detail ─────────────────────────
                elif selected_id.startswith("dnp_sub_"):
                    promo_id = selected_id[len("dnp_sub_"):].upper()
                    await _send_dnp_promo_detail(sender_id, promo_id)
                    return

                # 1b. Sheet-driven navigation — category selected (shcat_N)
                elif selected_id.startswith("shcat_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    id_map = nav.get("id_map", {})
                    cat_name = id_map.get(selected_id, serviceitems_text)
                    main_menu = nav.get("main_menu", "Bali Handbook")
                    subs = await get_sheet_menu_subcategories(main_menu, cat_name)
                    if subs:
                        sub_id_map = {}
                        rows = []
                        for i, s in enumerate(subs):
                            sid = f"shsub_{i}"
                            sub_id_map[sid] = s["subcategory"]
                            ep = s.get("endpoint", "")
                            desc = ep if ep and not ep.startswith("http") and len(ep) <= 69 else "Tap to explore"
                            rows.append({"id": sid, "title": s["subcategory"][:24], "description": desc})
                        sheet_nav_sessions[sender_id] = {
                            "main_menu": main_menu,
                            "category": cat_name,
                            "id_map": sub_id_map,
                        }
                        card_data = {
                            "main_title": cat_name[:60],
                            "main_description": f"Choose a topic from {cat_name}",
                            "data": rows,
                        }
                        await send_whatsapp_menu_list_message(sender_id, card_data)
                    else:
                        # No subcategories — execute endpoint directly
                        endpoint = await get_sheet_menu_endpoint(main_menu, cat_name)
                        await _execute_sheet_endpoint(sender_id, endpoint, cat_name, main_menu)
                    return

                # 1c. Sheet-driven navigation — subcategory selected (shsub_N)
                elif selected_id.startswith("shsub_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    id_map = nav.get("id_map", {})
                    sub_name = id_map.get(selected_id, serviceitems_text)
                    main_menu = nav.get("main_menu", "Bali Handbook")
                    category = nav.get("category", "")
                    # Check for 3rd navigation level before executing endpoint
                    subsubs = await get_sheet_menu_sub_subcategories(main_menu, category, sub_name)
                    if subsubs:
                        sub2_id_map = {}
                        rows = []
                        for i, s in enumerate(subsubs):
                            sid = f"shsub2_{i}"
                            sub2_id_map[sid] = s["sub_subcategory"]
                            rows.append({"id": sid, "title": s["sub_subcategory"][:24], "description": "Tap to explore"})
                        sheet_nav_sessions[sender_id] = {
                            "main_menu": main_menu, "category": category,
                            "subcategory": sub_name, "id_map": sub2_id_map,
                        }
                        await send_whatsapp_menu_list_message(sender_id, {
                            "main_title": sub_name, "main_description": "Choose an option:", "data": rows,
                        })
                    else:
                        endpoint = await get_sheet_menu_endpoint(main_menu, category, sub_name)
                        await _execute_sheet_endpoint(sender_id, endpoint, sub_name, main_menu)
                    return

                # 1d. Sheet-driven navigation — sub-subcategory selected (shsub2_N)
                elif selected_id.startswith("shsub2_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    id_map = nav.get("id_map", {})
                    subsub_name = id_map.get(selected_id, serviceitems_text)
                    main_menu = nav.get("main_menu", "Bali Handbook")
                    category = nav.get("category", "")
                    subcategory = nav.get("subcategory", "")
                    endpoint = await get_sheet_menu_endpoint(main_menu, category, subcategory, subsub_name)
                    await _execute_sheet_endpoint(sender_id, endpoint, subsub_name, main_menu)
                    return

                # ── Order Services: category selected (os_cat_N) ─────────────────
                elif selected_id.startswith("os_cat_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    cat_name = nav.get("id_map", {}).get(selected_id, serviceitems_text)
                    try:
                        _os_villa_code = await get_user_villa_code(sender_id)
                        from app.services.menu_services import get_sub_category as _get_sub_cat
                        subcats = await _get_sub_cat(cat_name, villa_code=_os_villa_code)
                        if subcats:
                            _sub_id_map = {}
                            _sub_rows = []
                            for _i, _sub in enumerate(subcats):
                                _sname = _sub.get("subcategory", "")
                                if not _sname:
                                    continue
                                _sid = f"os_sub_{_i}"
                                _sub_id_map[_sid] = _sname
                                _desc = str(_sub.get("description", "Tap to explore"))
                                _sub_rows.append({"id": _sid, "title": _sname[:24], "description": _desc[:69]})
                            sheet_nav_sessions[sender_id] = {
                                "main_menu": "Order Services", "category": cat_name, "id_map": _sub_id_map
                            }
                            _card = {"main_title": cat_name, "main_description": "Choose a type:", "data": _sub_rows}
                            await send_whatsapp_menu_list_message(sender_id, _card)
                        else:
                            # No subcategories — go straight to service items
                            from app.services.menu_services import get_service_items as _get_svc_items
                            _items = await _get_svc_items(cat_name, villa_code=_os_villa_code)
                            if _items:
                                _svc_rows = [{"title": i["service_item"], "button": i["button"]} for i in _items]
                                await send_whatsapp_service_list_message(sender_id, _svc_rows, cat_name)
                            else:
                                await send_whatsapp_message(sender_id, f"No services found for {cat_name}. Please try another category.")
                    except Exception as _e:
                        logger.error(f"os_cat handler error: {_e}")
                        await send_whatsapp_message(sender_id, "Sorry, couldn't load services. Please try again.")
                    return

                # ── Order Services: subcategory selected (os_sub_N) ──────────────
                elif selected_id.startswith("os_sub_"):
                    nav = sheet_nav_sessions.get(sender_id, {})
                    sub_name = nav.get("id_map", {}).get(selected_id, serviceitems_text)
                    try:
                        _os_villa_code2 = await get_user_villa_code(sender_id)
                        from app.services.menu_services import get_service_items as _get_svc_items2
                        _items = await _get_svc_items2(sub_name, villa_code=_os_villa_code2)
                        if _items:
                            _svc_rows = [{"title": i["service_item"], "button": i["button"]} for i in _items]
                            await send_whatsapp_service_list_message(sender_id, _svc_rows, sub_name)
                        else:
                            await send_whatsapp_message(sender_id, f"No services found for {sub_name}. Please try another option.")
                    except Exception as _e:
                        logger.error(f"os_sub handler error: {_e}")
                        await send_whatsapp_message(sender_id, "Sorry, couldn't load services. Please try again.")
                    return

                # 2. Handle Subcategory Selection (leads to Service Items list)
                elif selected_id.startswith("subcat_"):
                    subcategory_title = serviceitems_text
                    api_url = f"{settings.BASE_URL}/sub_category/service_items"
                    try:
                        _subcat_vc = await get_user_villa_code(sender_id)
                        async with httpx.AsyncClient() as client:
                            response = await client.post(api_url, json={"subcategory_title": subcategory_title, "villa_code": _subcat_vc or ""})
                            if response.status_code == 200:
                                service_items = response.json().get("serviceitems", [])
                                await send_whatsapp_service_list_message(sender_id, service_items, subcategory_title)
                                return
                    except Exception as e:
                        print(f"Error fetching service items: {e}")

                # 3. Handle Service Item Selection (triggers 'Book Now' Flow)
                elif selected_id.startswith("service_"):
                    service_name = serviceitems_text

                    # Start interactive step-by-step booking
                    _bk_price = ""
                    _pre_villa_code = None
                    try:
                        _pre_villa_code = await get_user_villa_code(sender_id)
                        _bp = await get_location_specific_price(service_name, _pre_villa_code)
                        if _bp:
                            _bp_clean = int(re.sub(r'[^\d]', '', str(_bp)) or '0')
                            _bk_price = f"IDR {_bp_clean:,}"
                    except Exception:
                        pass
                    # Villa-code JIT gate — ask first if unknown, same as Order Services tap
                    if not _pre_villa_code and await _gate_booking_on_villa_code(sender_id, service_name, _bk_price):
                        return
                    await _start_booking_flow(sender_id, service_name, _bk_price)
                    return

                # 4. Handle AI-generated menu (Legacy/Search flow)
                elif selected_id.startswith("ai_service_"):
                    # Get full service details
                    service_details = await ai_menu_generator.get_service_details_by_id(selected_id)
                    
                    if service_details:
                        service_name = service_details["service_name"]
                        description = service_details["description"]
                        price = service_details["price"]
                        locations = service_details["locations"]
                        image_url = service_details["image_url"]

                        # Format price properly
                        try:
                            price_num = int(str(price).replace(' ', '').replace(',', ''))
                            price_formatted = f"IDR {price_num:,}"
                        except:
                            price_formatted = f"IDR {price}"
                        
                        # Send booking prompt
                        booking_message = (
                            f"💰 *Price:* {price_formatted}\n"
                            f"📍 *Available in:* {locations}\n\n"
                            f"Would you like to book this service? Use the button below to continue 👇"
                        )
                        await send_whatsapp_message(sender_id, booking_message)
                        
                        # Start interactive step-by-step booking
                        await _start_booking_flow(sender_id, service_name, price_formatted)
                        return
                    else:
                        await send_whatsapp_message(
                            sender_id, 
                            "Sorry, I couldn't find the details for this service. Please try again or select another option."
                        )
                        return

            elif interactive_type == "nfm_reply": # This is the type for Flow replies
                nfm_reply = message_payload["interactive"]["nfm_reply"]
                response_json_str = nfm_reply.get("response_json", "{}")

                try:
                    raw = json.loads(response_json_str)
                except json.JSONDecodeError as e:
                    print(f"❌ Error decoding NFM reply JSON: {e}")
                    await send_whatsapp_message(sender_id, "Sorry, there was an issue processing your selection. Please try again.")
                    return

                # Decrypt if encrypted (published flows)
                if "encrypted_flow_data" in raw:
                    try:
                        from app.services.whatsapp_flows_service import decrypt_flow_response
                        response_data = decrypt_flow_response(
                            raw["encrypted_flow_data"],
                            raw["encrypted_aes_key"],
                            raw["initial_vector"],
                        )
                    except Exception as dec_err:
                        logger.error(f"Flow decryption failed: {dec_err}")
                        # SOLUTION 2: Smart fallback — send customer the web booking link
                        # so the booking is never silently lost due to a key mismatch
                        import urllib.parse as _up
                        # Try to get service name from any active booking session
                        _bk_sess = await _get_booking_session(sender_id)
                        _svc = _bk_sess.get("service_name", "") if _bk_sess else ""
                        _price = _bk_sess.get("price", "0") if _bk_sess else "0"
                        _raw_price = re.sub(r'[^\d]', '', str(_price)) or "0"
                        if _svc:
                            _url = (
                                f"{settings.WEB_BASE_URL}/book"
                                f"?service={_up.quote(_svc)}&price={_raw_price}&wa={sender_id}"
                            )
                            await send_whatsapp_message(
                                sender_id,
                                f"⚠️ We had a small issue processing your form submission.\n\n"
                                f"No worries — please use the link below to complete your booking "
                                f"for *{_svc}*. It only takes 30 seconds! 👇\n\n{_url}"
                            )
                            await send_whatsapp_interactive_link_with_text(
                                sender_id, _url, "📋 Complete Booking", f"Book {_svc}"
                            )
                        else:
                            await send_whatsapp_message(
                                sender_id,
                                "⚠️ We had a small issue processing your form. "
                                "Please select your service again from the menu and try once more."
                            )
                        return
                else:
                    response_data = raw

                flow_token = response_data.get("flow_token")

                # ── Guest Registration Flow Handling ──────────────────────────
                if flow_token and flow_token.startswith("reg_"):
                    # Process Guest Registration
                    f_name = response_data.get("full_name")
                    v_code = str(response_data.get("villa_code", "")).strip().upper()
                    c_in = response_data.get("check_in_date")
                    c_out = response_data.get("check_out_date")
                    
                    # Fetch villa info for better context
                    v_info = await get_villa_info_by_code(v_code)
                    v_name = v_info.get("name") if v_info else "Unknown Villa"
                    v_loc = v_info.get("location") if v_info else "Bali"
                    
                    # Store in guest_profile_collection using phone_number as key
                    new_profile = GuestProfile(
                        phone_number=sender_id,
                        full_name=f_name,
                        villa_code=v_code,
                        villa_name=v_name,
                        location_zone=v_loc,
                        check_in_date=c_in,
                        check_out_date=c_out,
                        source="whatsapp"
                    )
                    
                    await guest_profile_collection.update_one(
                        {"phone_number": sender_id},
                        {"$set": new_profile.model_dump()},
                        upsert=True
                    )
                    
                    # Update customer record
                    await customer_collection.update_one(
                        {"phone": sender_id},
                        {"$set": {"name": f_name, "villa_code": v_code}}
                    )
                    
                    # Save villa code to session for location-aware pricing
                    await save_user_villa_code(sender_id, v_code, source="registration")
                    
                    # Success message
                    await send_whatsapp_message(
                        sender_id,
                        f"Registration successful! Welcome, *{f_name}*. 🌴\n\n"
                        "You can now use all our services. Type *menu* to see what we offer!"
                    )
                    return
                # ────────────────────────────────────────────────────────────

                # ── Venue Setup Flow COMPLETE (unregistered user villa picker) ─
                if flow_token and flow_token.startswith("venue_"):
                    _vs_villa_code = str(response_data.get("villa_code") or response_data.get("selected_villa", "")).strip().upper()
                    _vs_location = response_data.get("selected_location", "")
                    logger.info(f"Venue setup nfm_reply: villa_code={_vs_villa_code}, location={_vs_location}")

                    if _vs_villa_code and _vs_villa_code != "UNLISTED":
                        # Save villa code
                        await save_user_villa_code(sender_id, _vs_villa_code, source="venue_setup_flow")

                        # Look up villa name for welcome message
                        _vs_villa_info = await get_villa_info_by_code(_vs_villa_code)
                        _vs_villa_name = (_vs_villa_info or {}).get("name", "your villa")
                        if not _vs_location:
                            _vs_location = (_vs_villa_info or {}).get("location", "")

                        # Upsert minimal guest profile
                        try:
                            import datetime as _vs_dt
                            _vs_now = _vs_dt.datetime.utcnow()
                            await guest_profile_collection.update_one(
                                {"sender_id": sender_id},
                                {"$set": {
                                    "sender_id": sender_id,
                                    "phone_number": sender_id,
                                    "villa_code": _vs_villa_code,
                                    "villa_name": _vs_villa_name,
                                    "location_zone": _vs_location,
                                    "source": "whatsapp_venue_setup_flow",
                                    "updated_at": _vs_now,
                                }, "$setOnInsert": {"created_at": _vs_now}},
                                upsert=True,
                            )
                        except Exception as _vs_gp_err:
                            logger.error(f"Venue setup: guest profile upsert failed (non-fatal): {_vs_gp_err}")

                        # Create active check-in record so automated guest
                        # sequences (passport reminder, day-1 welcome, ...) fire
                        await ensure_active_checkin(
                            sender_id, _vs_villa_code, _vs_villa_name, _vs_location
                        )

                        # Send Order Services category flow
                        try:
                            import uuid as _vs_uuid
                            from app.services.whatsapp_flows_service import send_category_flow_message as _vs_cat_flow
                            _vs_cat_token = f"cat_{sender_id}_{_vs_uuid.uuid4().hex[:8]}"
                            _vs_cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                            await _vs_cat_flow(sender_id, _vs_cat_flow_id, _vs_cat_token)
                            logger.info(f"Venue setup COMPLETE: Order Services flow sent to {sender_id}")
                        except Exception as _vs_cf_err:
                            logger.error(f"Venue setup: category flow send failed (non-fatal): {_vs_cf_err}")
                            await send_whatsapp_message(
                                sender_id,
                                f"Great! We've saved *{_vs_villa_name}* as your villa. "
                                "Type *Order Services* to browse and book services."
                            )
                    else:
                        await send_whatsapp_message(
                            sender_id,
                            "We couldn't identify your villa. Please try again or type your villa code (e.g. V1)."
                        )
                    return
                # ─────────────────────────────────────────────────────────────

                # ── DNP Flow COMPLETE (Discounts & Promotions) ─────────────
                # When the guest closes the DNP flow, Meta delivers an nfm_reply
                # with a flow_token starting with "dnp_".  Send a fresh "View Deals"
                # card so the guest can browse again (WhatsApp seals completed cards).
                if flow_token and flow_token.startswith("dnp_"):
                    try:
                        import uuid as _dnp_uuid
                        from app.services.whatsapp_flows_service import send_dnp_flow_message as _re_send_dnp
                        _dnp_new_token = f"dnp_{sender_id[:20]}_{_dnp_uuid.uuid4().hex[:8]}"
                        await _re_send_dnp(sender_id, _dnp_new_token)
                        logger.info(f"DNP flow completed — fresh flow card sent to {sender_id}")
                    except Exception as _dnp_re_err:
                        logger.warning(f"DNP re-send failed (non-fatal): {_dnp_re_err}")
                        await send_whatsapp_message(
                            sender_id,
                            "Tap *Discounts & Promotions* from the menu to browse deals again."
                        )
                    return
                # ─────────────────────────────────────────────────────────────

                # New booking form fields
                full_name    = response_data.get("full_name", "")
                phone_number = response_data.get("phone_number", "")
                booking_date = response_data.get("booking_date", "")  # ISO date or epoch ms from CalendarPicker
                time_slot    = response_data.get("time_slot", "")
                persons      = response_data.get("persons", "1")

                # Backward compat with old flow fields
                if not booking_date:
                    booking_date = response_data.get("selected_date", "")
                if not booking_date:
                    booking_date = response_data.get("calendar", "")
                if not time_slot:
                    time_slot = response_data.get("time_selection", "Flexible")
                if not persons:
                    persons = response_data.get("person_selection", "1")

                # full_name comes from the BOOKING screen TextInput.
                # For returning guests whose name is already stored, also fall back to DB
                # so orders always carry a customer name even if the form was skipped.
                if not full_name:
                    _gp = await guest_profile_collection.find_one({"phone_number": sender_id})
                    if _gp:
                        full_name = _gp.get("full_name", "")
                    if not full_name:
                        _cu = await customer_collection.find_one({"phone": sender_id})
                        if _cu:
                            full_name = _cu.get("name", "")

                # Look up service from booking session by flow_token
                selected_service = ""
                price_str = ""
                if flow_token:
                    bk_sess = await booking_sessions_collection.find_one_and_delete({"flow_token": flow_token})
                    if bk_sess:
                        selected_service = bk_sess.get("service_name", "")
                        price_str = bk_sess.get("price", "")

                # Fallback to response data (old flow format)
                if not selected_service:
                    selected_service = response_data.get("selected_service", "")

                # Handle AI service name encoding
                if str(selected_service).startswith("ai_service_"):
                    parts = str(selected_service).split("_", 3)
                    if len(parts) >= 4:
                        selected_service = parts[3].replace('_', ' ')

                # Format booking_date string
                booking_date_str = ""
                user_date = datetime.datetime.now()
                if booking_date:
                    try:
                        if str(booking_date).isdigit():
                            user_date = datetime.datetime.fromtimestamp(int(booking_date) / 1000)
                            booking_date_str = user_date.strftime("%d/%m/%Y")
                        else:
                            user_date = dateutil.parser.parse(str(booking_date), dayfirst=False)
                            booking_date_str = user_date.strftime("%d/%m/%Y")
                    except Exception:
                        booking_date_str = str(booking_date)

                logger.info(f"✨ Booking flow: service='{selected_service}', name='{full_name}', date='{booking_date_str}', time='{time_slot}', persons='{persons}'")

                if not selected_service:
                    logger.error(f"❌ NFM reply missing service: flow_token={flow_token}")
                    await send_whatsapp_message(sender_id, "⚠️ We couldn't identify the service. Please try again from the menu.")
                    return

                # Validate required fields from form
                if not full_name or not full_name.strip():
                    await send_whatsapp_message(sender_id, "⚠️ We didn't receive your name. Please try booking again from the menu.")
                    return
                if not booking_date_str:
                    await send_whatsapp_message(sender_id, "⚠️ We didn't receive a booking date. Please try booking again from the menu.")
                    return

                try:
                    _nfm_ph = re.sub(r"\s+", "", phone_number) if phone_number else None

                    # WCR-VILLA-MISMATCH-01 (2026-08-25): prefer the villa_code the
                    # guest actually browsed and confirmed through THIS Category
                    # Flow session (embedded end-to-end from handle_category_flow_init
                    # through the BOOKING screen, echoed back here in response_data)
                    # over an independent re-resolution. resolve_customer_context()
                    # prioritises villa_code_collection (QR scan / manual entry),
                    # which can disagree with customer_collection -- the source
                    # handle_category_flow_init actually used to decide which
                    # villa's services the guest was shown. Silently re-resolving
                    # here let the order (and therefore the SP notification) end up
                    # attributed to a DIFFERENT villa than the one the guest was
                    # just browsing (reported live: guest in V3, SP notification
                    # said V4). Falls back to resolve_customer_context only when the
                    # flow itself carried nothing (older flow format, or the guest's
                    # browsing session never resolved a villa either).
                    _flow_villa_code = str(response_data.get("villa_code") or "").strip().upper()
                    if _flow_villa_code and re.match(r"^V\d+$", _flow_villa_code):
                        _nfm_villa_code = _flow_villa_code
                        _nfm_location = str(response_data.get("location_zone") or "")
                        _nfm_ctx_source = "category_flow_session"
                    else:
                        # DB-authoritative fallback — shared resolver, same as main WA booking path
                        from app.services.customer_context import resolve_customer_context as _rcc, CustomerContextError as _CCE
                        _nfm_ctx = await _rcc(
                            sender_id=sender_id,
                            phone_number=_nfm_ph if _nfm_ph and _nfm_ph != sender_id else None,
                            payload_villa_code=None,  # WA: no URL param, DB is only source
                        )
                        if isinstance(_nfm_ctx, _CCE):
                            logger.warning(f"NFM booking blocked: {_nfm_ctx.code} for {sender_id}")
                            await send_whatsapp_message(
                                sender_id,
                                "We couldn't determine your villa. Please send your Villa Code (e.g. V1) so we can complete your booking."
                            )
                            await _save_vc_session(sender_id, "pending")
                            return
                        _nfm_villa_code = _nfm_ctx.villa_code
                        _nfm_location   = _nfm_ctx.location_zone or ""
                        _nfm_ctx_source = _nfm_ctx.source

                    base_price = await get_location_specific_price(selected_service, _nfm_villa_code)
                    new_order = await initiate_chat_session(
                        sender_id=sender_id,
                        service_name=selected_service,
                        person_count=persons,
                        base_price=base_price,
                        date=user_date,
                        time=time_slot,
                    )
                    new_order.date   = user_date
                    new_order.time   = time_slot
                    new_order.status = "pending"

                    order_dict = new_order.dict()
                    order_dict["confirmation"]         = False
                    order_dict["customer_id"]          = customer_id
                    order_dict["customer_name"]        = full_name.strip()
                    order_dict["phone_number"]         = _nfm_ph or sender_id
                    order_dict["booking_date"]         = booking_date_str
                    order_dict["persons"]              = persons
                    order_dict["villa_code"]           = _nfm_villa_code
                    order_dict["location_zone"]        = _nfm_location
                    order_dict["villa_context_source"] = _nfm_ctx_source
                    try:
                        _nfm_villa_info_pre = await get_villa_info_by_code(_nfm_villa_code)
                        order_dict["villa_name"] = (_nfm_villa_info_pre or {}).get("name", "")
                    except Exception:
                        order_dict["villa_name"] = ""
                    from app.models.order_summary import PayoutStatus as _PS, BookingStatus as _BS
                    order_dict["payout_status"]        = _PS.PENDING
                    order_dict["booking_status"]       = _BS.AWAITING_SP_CONFIRMATION
                    await save_order_to_db(order_dict)
                    logger.info(f"NFM order {new_order.order_number} created: villa={_nfm_villa_code}, loc={_nfm_location}, src={_nfm_ctx_source}")

                    # Persist guest identity from BOOKING form — non-fatal, order is already saved
                    try:
                        _bk_name = full_name.strip() if full_name else ""
                        _bk_phone_raw = _nfm_ph or sender_id
                        # Normalise phone: strip spaces/dashes/parens, ensure leading digits only
                        _bk_phone = re.sub(r"[\s\-\(\)\.\+]", "", _bk_phone_raw).lstrip("0")
                        if _bk_phone and not _bk_phone.startswith("62"):
                            _bk_phone = "62" + _bk_phone
                        if not _bk_phone:
                            _bk_phone = sender_id
                        if _nfm_villa_code:
                            _bk_villa_info = await get_villa_info_by_code(_nfm_villa_code)
                            _bk_villa_name = (_bk_villa_info or {}).get("name", "")
                            _now_bk = datetime.datetime.utcnow()
                            # Build the $set payload — only include full_name/phone if we have them
                            # so we never overwrite a known name with an empty string.
                            _gp_set = {
                                "sender_id": sender_id,
                                "villa_code": _nfm_villa_code,
                                "villa_name": _bk_villa_name,
                                "location_zone": _nfm_location,
                                "source": "whatsapp_booking",
                                "updated_at": _now_bk,
                            }
                            if _bk_phone:
                                _gp_set["phone_number"] = _bk_phone
                            if _bk_name:
                                _gp_set["full_name"] = _bk_name
                            await guest_profile_collection.update_one(
                                {"$or": [{"sender_id": sender_id}, {"phone_number": _bk_phone}]} if _bk_phone else {"sender_id": sender_id},
                                {"$set": _gp_set, "$setOnInsert": {"created_at": _now_bk}},
                                upsert=True,
                            )
                            if _bk_name and _bk_phone:
                                await customer_collection.update_one(
                                    {"$or": [{"phone": sender_id}, {"phone": _bk_phone}]},
                                    {"$set": {
                                        "name": _bk_name,
                                        "phone": _bk_phone,
                                        "villa_code": _nfm_villa_code,
                                        "villa_name": _bk_villa_name,
                                        "location_zone": _nfm_location,
                                    }},
                                    upsert=True,
                                )
                            await save_user_villa_code(sender_id, _nfm_villa_code, source="booking_form")
                            logger.info(f"NFM guest profile upserted for {sender_id}: name={_bk_name}, villa={_nfm_villa_code}, phone={_bk_phone}")
                    except Exception as _bk_prof_err:
                        logger.warning(f"NFM guest profile upsert failed (non-fatal): {_bk_prof_err}")

                    try:
                        price_cleaned = int(re.sub(r'[^\d]', '', str(new_order.price)) or '0')
                        num_p = int(persons) if str(persons).isdigit() else 1
                        price_display = f"IDR {price_cleaned * num_p:,}"
                    except Exception:
                        price_display = price_str or f"IDR {new_order.price}"

                    try:
                        _conf_villa_info = await get_villa_info_by_code(_nfm_villa_code)
                        _conf_villa_name = (_conf_villa_info or {}).get("name") or _nfm_villa_code
                    except Exception:
                        _conf_villa_name = _nfm_villa_code

                    booking_summary_block = (
                        f"🎉 *Booking Request Received!*\n\n"
                        f"Here's your booking summary:\n"
                        f"──────────────────────\n"
                        f"📋 *Order ID:* `{new_order.order_number}`\n"
                        f"🧖 *Service:* {new_order.service_name}\n"
                        f"🏡 *Villa:* {_conf_villa_name}\n"
                        f"👤 *Name:* {full_name.strip()}\n"
                        f"📅 *Date:* {booking_date_str}\n"
                        f"⏰ *Time:* {time_slot}\n"
                        f"💰 *Total:* {price_display}\n"
                        f"──────────────────────\n\n"
                    )

                    # Resolve + attempt SP notification BEFORE telling the guest anything
                    # was confirmed — the guest must never be told "a provider has been
                    # notified" until that has actually been attempted and we know the
                    # outcome. See CLAUDE.md "SP Notification Structural Integrity".
                    # Location already resolved — use directly for SP routing
                    service_numbers = await fetch_whatsapp_numbers(selected_service, _nfm_location)
                    logger.info(f"NFM: notifying {len(service_numbers)} SPs in {_nfm_location}: {service_numbers}")
                    sp_notification_results = []
                    _nfm_notified_numbers = []
                    for num in service_numbers:
                        try:
                            result = await send_whatsapp_order_to_SP(num, order_dict)
                            sp_notification_results.append({"number": num, "success": result is not None, "response": str(result)})
                            _nfm_notified_numbers.append(num)
                            logger.info(f"SP notify {num}: {'OK' if result else 'FAILED'}")
                        except Exception as _sp_err:
                            sp_notification_results.append({"number": num, "success": False, "error": str(_sp_err)})
                            logger.error(f"Failed to notify SP {num}: {_sp_err}")
                    from app.db.session import order_collection as _oc
                    await _oc.update_one(
                        {"order_number": new_order.order_number},
                        {"$set": {
                            "sp_notifications": sp_notification_results,
                            "sp_notified_at": datetime.datetime.now(),
                            "sp_notified_numbers": _nfm_notified_numbers,
                        }}
                    )

                    if _nfm_notified_numbers:
                        confirmation_message = booking_summary_block + (
                            f"⏳ A service provider has been notified and will confirm shortly.\n\n"
                            f"Once confirmed, your *secure payment link* will appear right here. Please keep this chat open! 🔔"
                        )
                    else:
                        confirmation_message = booking_summary_block + (
                            f"⏳ We're arranging a service provider for your booking — this may take a little longer than usual.\n\n"
                            f"We'll follow up here as soon as it's confirmed. 🔔"
                        )
                        # No SP could be reached at booking time — this must never sit
                        # silently in a log file. Alert admin so it gets manual follow-up.
                        try:
                            admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
                            await send_whatsapp_message(
                                admin_number,
                                f"🚨 *Order {new_order.order_number} — no SP notified*\n\n"
                                f"Service: *{selected_service}*\n"
                                f"Villa: `{_nfm_villa_code}`\n"
                                f"Guest: `{sender_id[-4:]}`\n\n"
                                f"No provider could be reached at booking time. Manual follow-up required."
                            )
                        except Exception as _admin_err:
                            logger.error(f"Failed to send no-SP admin alert for {new_order.order_number}: {_admin_err}")

                    await send_whatsapp_message(sender_id, confirmation_message)
                    return

                except Exception as booking_err:
                    import traceback
                    logger.error(f"❌ Booking flow error: {booking_err}\n{traceback.format_exc()}")
                    await send_whatsapp_message(
                        sender_id,
                        "Sorry, we encountered an issue. Please try again or type *menu* to start over."
                    )
                    return

        # ── Template Quick Reply handler ─────────────────────────────────────────
        # When an SP taps Accept/Decline on a template message, WhatsApp sends
        # type="button" (NOT type="interactive"), with payload instead of id.
        elif "button" in message_payload:
            btn_payload = message_payload["button"].get("payload", "")
            btn_text = message_payload["button"].get("text", "")
            logger.info(f"Template button tap from {sender_id}: text='{btn_text}' payload='{btn_payload}'")

            if btn_text == "Accept":
                order_num = btn_payload
                # Check order status BEFORE processing — reject timed-out or cancelled orders
                _sp_order_doc = await order_collection.find_one({"order_number": order_num})
                if _sp_order_doc:
                    _bk_status = _sp_order_doc.get("booking_status", "")
                    _legacy_status = _sp_order_doc.get("status", "")
                    _is_closed = _bk_status in ("FAILED", "CANCELLED", "COMPLETED") or _legacy_status in ("sp_timeout", "cancelled", "CANCELLED", "completed")
                    if _is_closed:
                        await send_whatsapp_message(
                            sender_id,
                            f"⏰ *This booking has expired.*\n\n"
                            f"Order *{order_num}* was cancelled because no service provider "
                            f"accepted within the required time. The guest has been notified.\n\n"
                            f"Thank you for your response!"
                        )
                        return
                service_provider_code = await get_service_provider_by_whatsapp(sender_id)
                await order_collection.update_one(
                    {"order_number": order_num},
                    {"$set": {"service_provider_code": service_provider_code}}
                )
                local_order_store[sender_id] = order_num
                session_id = await get_sender_id_by_order(order_num)
                confirmation = await check_order_confirmation(order_num)
                if not confirmation:
                    await send_confirmation_order_to_SP(sender_id, order_num)
                else:
                    await send_whatsapp_message(
                        sender_id,
                        "Thank you for the acceptance. Unfortunately, this order has already been booked."
                    )
                return

            elif btn_text == "Decline" and btn_payload.startswith("decline_"):
                order_num = btn_payload.split("_", 1)[1]
                decline_sessions[sender_id] = order_num
                await send_decline_confirmation(sender_id, order_num)
                return

        if message_text and re.search(r"Hi,?\s*I\s*am\s*in", message_text, re.IGNORECASE):    
            try:
                villa_name = extract_villa_name (message_text)
                villa_code = await get_villa_code_by_name(villa_name)
                
                if villa_code:
                    success = await save_user_villa_code(sender_id, villa_code, source="qr_scan")
                    if success:
                        await perform_arrival_confirmation(sender_id, villa_code, customer_id)
                        await send_whatsapp_message(sender_id, "Hi")
                        await starting_message(sender_id)
                        return
                    else:
                        await send_whatsapp_message(
                            sender_id,
                            "❌ Sorry, there was an error setting up your villa access. Please try the link again."
                        )
                        return
                else:
                    # Villa name not found in database mapping
                    await send_whatsapp_message(
                        sender_id,
                        f"❌ *Villa '{villa_name}' not recognized.*\n\n"
                        "Please ensure you are using the correct link provided in your villa, or contact support if the issue persists."
                    )
                    return
                    
            except Exception as e:
                print(f"Error processing villa initialization: {e}")
                await send_whatsapp_message(
                    sender_id,
                    "❌ There was an error processing your request. Please try again."
                )
                return

        user_villa_code = await get_user_villa_code(sender_id)

        # ── Greeting handler — runs for ALL users regardless of villa code ────────
        # New users (no villa code) must see the main menu on "Hi", not AI chat.
        # This must come before the if/else villa_code split.
        _GREETING_WORDS = {
            "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
            "hey there", "hi there", "hello there", "howdy", "what's up?",
            "how are you?", "how's it going?", "yo", "greetings", "bonjour",
            "menu", "start", "home", "main menu",
        }
        _msg_lower_g = message_text.strip().lower() if message_text else ""
        # Also catch longer phrases like "can you send me the main menu pls" or
        # "show me the menu" — substrings not in _GREETING_WORDS exact-match set.
        # Safe: service requests say "massage menu"/"shisha menu" (never "the menu").
        _MENU_SUBSTRINGS = ("main menu", "the menu")
        if message_text and (
            _msg_lower_g in _GREETING_WORDS
            or any(sub in _msg_lower_g for sub in _MENU_SUBSTRINGS)
        ):
            # Clear any stale sessions so the menu is clean
            issue_reporting_sessions.pop(sender_id, None)
            onboarding_sessions.pop(sender_id, None)
            persistent_mode_sessions.pop(sender_id, None)
            feedback_sessions.pop(sender_id, None)
            language_lesson_sessions.pop(sender_id, None)
            amenity_wa_sessions.pop(sender_id, None)
            dnp_wa_sessions.pop(sender_id, None)
            await _del_vc_session(sender_id)
            await _delete_booking_session(sender_id)
            await starting_message(sender_id)
            return
        # ─────────────────────────────────────────────────────────────────────────

        # Users without a villa code can browse freely — villa is collected JIT
        # when they tap Order Services. Session value "order_services" means the
        # Category Flow should launch after the code is saved (not the main menu).
        _vc_session_raw = await _get_vc_session(sender_id)
        if _vc_session_raw is not None and message_text:
            _vc_input = message_text.strip()

            # "no" / "I don't know" → send Venue Setup Flow (location + villa picker)
            _no_villa_words = {
                "no", "nope", "nah", "don't know", "dont know", "i don't know", "i dont know",
                "not sure", "no idea", "idk", "dunno", "unknown", "don't have", "dont have",
                "i don't have it", "no code", "no villa code", "help me find villa", "help me find my villa"
            }
            if _vc_input.lower() in _no_villa_words or _vc_input.lower().startswith("no ") or any(_w in _vc_input.lower() for _w in _no_villa_words):
                logger.info(f"No-villa-code reply from {sender_id}, sending Venue Setup Flow")
                await _del_vc_session(sender_id)
                import uuid as _uuid_no
                _no_vs_token = f"venue_{sender_id}_{_uuid_no.uuid4().hex[:8]}"
                from app.services.whatsapp_flows_service import send_venue_setup_flow_message as _send_venue_no
                try:
                    await _send_venue_no(sender_id, _no_vs_token)
                except Exception as _no_vs_err:
                    logger.error(f"Venue setup flow send failed for {sender_id}: {_no_vs_err}")
                    await send_whatsapp_message(
                        sender_id,
                        "No problem! Please share your area (e.g. Seminyak, Canggu) "
                        "and we'll show you the villas there."
                    )
                return

            # Allow escape words to exit the villa code collection loop
            _vc_escape = _vc_input.lower() in ("hi", "hello", "hey", "menu", "start", "help", "back", "cancel", "0")
            if _vc_escape:
                await _del_vc_session(sender_id)
                await starting_message(sender_id)
                return

            _vc_session = _vc_session_raw
            _vc_ctx = _vc_session if isinstance(_vc_session, str) else (_vc_session or {}).get("ctx", "order_services")

            # ── Step 1b: Guest picked a location from the location list ──────────
            if isinstance(_vc_session, dict) and _vc_session.get("step") == "awaiting_location_choice":
                _loc_offered = _vc_session.get("locations", [])
                _loc_pick = _vc_input.strip()
                _picked_location = None
                if _loc_pick.isdigit():
                    _loc_idx = int(_loc_pick) - 1
                    if 0 <= _loc_idx < len(_loc_offered):
                        _picked_location = _loc_offered[_loc_idx]
                else:
                    _loc_lower = _loc_pick.lower()
                    for _l in _loc_offered:
                        if _loc_lower in _l.lower() or _l.lower() in _loc_lower:
                            _picked_location = _l
                            break
                _cannot_browse_msg = (
                    "To browse and book our services, we need to know which villa you're staying at "
                    "so we can show you the right options and pricing.\n\n"
                    "Please share your:\n"
                    "• *Villa code* (e.g. V1, V2) — on your welcome card or QR\n"
                    "• *Villa name* (e.g. Villa Manila)\n"
                    "• *Area* (e.g. Seminyak, Canggu)\n\n"
                    "Or type *Hi* to explore the general menu (Bali Handbook, Currency Converter, Voice Translator and more)."
                )
                if _picked_location:
                    from app.services.menu_services import get_all_villas as _get_all_villas_loc
                    _all_villas_loc = await _get_all_villas_loc()
                    _loc_villas = [v for v in _all_villas_loc if v.get("location", "").lower() == _picked_location.lower()]
                    if _loc_villas:
                        _villa_lines = "\n".join(f"{i+1}. {v['name']}" for i, v in enumerate(_loc_villas))
                        await _save_vc_session(sender_id, {"ctx": _vc_ctx, "step": "awaiting_villa_choice", "villas": _loc_villas, "ts": datetime.datetime.now().isoformat()})
                        await send_whatsapp_message(
                            sender_id,
                            f"Villas in *{_picked_location}*:\n\n{_villa_lines}\n\nReply with the *number* of your villa."
                        )
                    else:
                        # Location exists but no villas registered under it — stay in session, let them retry
                        await send_whatsapp_message(
                            sender_id,
                            f"I don't have any villas listed under *{_picked_location}* yet.\n\n"
                            + _cannot_browse_msg
                        )
                else:
                    # Unrecognised input — track attempts, give clearer exit after 2nd failure
                    _attempts = _vc_session.get("attempts", 0) + 1
                    if _attempts >= 2:
                        await _del_vc_session(sender_id)
                        await send_whatsapp_message(sender_id, _cannot_browse_msg)
                    else:
                        _loc_lines = "\n".join(f"{i+1}. {l}" for i, l in enumerate(_loc_offered))
                        await _save_vc_session(sender_id, {**_vc_session, "attempts": _attempts})
                        await send_whatsapp_message(
                            sender_id,
                            f"Please reply with a number from the list:\n\n{_loc_lines}\n\n"
                            "Or type *Hi* to go back to the main menu."
                        )
                return
            # ────────────────────────────────────────────────────────────────────

            # ── Step 2: Guest picked a villa from location list ──────────────────
            if isinstance(_vc_session, dict) and _vc_session.get("step") == "awaiting_villa_choice":
                _offered = _vc_session.get("villas", [])
                _pick = _vc_input.strip()
                _matched_vc = None
                # Try number pick (e.g. "1", "2")
                if _pick.isdigit():
                    _idx = int(_pick) - 1
                    if 0 <= _idx < len(_offered):
                        _matched_vc = _offered[_idx]["code"]
                else:
                    # Try name match within the offered list
                    _pick_lower = _pick.lower()
                    for _v in _offered:
                        if _pick_lower in _v["name"].lower() or _v["name"].lower() in _pick_lower:
                            _matched_vc = _v["code"]
                            break
                if _matched_vc:
                    villa_info = await get_villa_info_by_code(_matched_vc)
                    await save_user_villa_code(sender_id, _matched_vc, source="manual_entry")
                    await _del_vc_session(sender_id)
                    if _vc_ctx == "start_booking" and await _try_resume_pending_booking(sender_id):
                        return
                    if _vc_ctx == "order_services":
                        _cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                        if _cat_flow_id:
                            import uuid as _uuid_vc2
                            _vc2_flow_token = f"cat_{sender_id}_{_uuid_vc2.uuid4().hex[:8]}"
                            from app.services.whatsapp_flows_service import send_category_flow_message as _send_vc2_cat_flow
                            try:
                                await _send_vc2_cat_flow(sender_id, _cat_flow_id, _vc2_flow_token)
                            except Exception as _e2:
                                logger.error(f"Category flow send failed after villa choice for {sender_id}: {_e2}")
                                await send_whatsapp_message(sender_id, f"✅ Villa saved! Type *Order Services* to continue.")
                        else:
                            await send_whatsapp_message(sender_id, f"✅ Villa saved! Type *Order Services* to continue.")
                    else:
                        await send_whatsapp_message(sender_id, f"✅ Welcome to *{(villa_info or {}).get('name', 'your villa')}*! 🏡")
                        await starting_message(sender_id)
                    return
                else:
                    _vc2_attempts = (_vc_session.get("attempts", 0) if isinstance(_vc_session, dict) else 0) + 1
                    if _vc2_attempts >= 2:
                        await _del_vc_session(sender_id)
                        await send_whatsapp_message(
                            sender_id,
                            "To browse and book our services, we need to know which villa you're staying at "
                            "so we can show you the right options and pricing.\n\n"
                            "Please share your:\n"
                            "• *Villa code* (e.g. V1, V2) — on your welcome card or QR\n"
                            "• *Villa name* (e.g. Villa Manila)\n"
                            "• *Area* (e.g. Seminyak, Canggu)\n\n"
                            "Or type *Hi* to explore the general menu (Bali Handbook, Currency Converter, Voice Translator and more)."
                        )
                    else:
                        if isinstance(_vc_session, dict):
                            await _save_vc_session(sender_id, {**_vc_session, "attempts": _vc2_attempts})
                        _vc2_villas = _vc_session.get("villas", []) if isinstance(_vc_session, dict) else []
                        _vc2_lines = "\n".join(f"{i+1}. {v['name']}" for i, v in enumerate(_vc2_villas))
                        await send_whatsapp_message(
                            sender_id,
                            f"Please reply with a number from the list:\n\n{_vc2_lines}\n\n"
                            "Or type *Hi* to go back to the main menu."
                        )
                    return
            # ────────────────────────────────────────────────────────────────────

            villa_code = _vc_input.upper()

            # ── Valid V-code format → validate against sheet ─────────────────────
            if is_valid_villa_code(villa_code):
                villa_info = await get_villa_info_by_code(villa_code)
                if not villa_info:
                    await send_whatsapp_message(
                        sender_id,
                        "❌ That villa code is not recognised.\n\n"
                        "Don't know your code? Reply with your *villa name* or *area* "
                        "(e.g. \"Villa Manila\" or \"Seminyak\") and I'll look it up for you.\n\n"
                        "Or type *Hi* to go back to the main menu."
                    )
                    return
                success = await save_user_villa_code(sender_id, villa_code, source="manual_entry")
                if success:
                    _vc_session_ctx = await _get_vc_session(sender_id)
                    await _del_vc_session(sender_id)
                    _vc_session_ctx_str = _vc_session_ctx if isinstance(_vc_session_ctx, str) else (_vc_session_ctx or {}).get("ctx", "order_services")
                    if _vc_session_ctx_str == "start_booking" and await _try_resume_pending_booking(sender_id):
                        return
                    if _vc_session_ctx_str == "order_services":
                        _cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                        if _cat_flow_id:
                            import uuid as _uuid_vc
                            _vc_flow_token = f"cat_{sender_id}_{_uuid_vc.uuid4().hex[:8]}"
                            from app.services.whatsapp_flows_service import send_category_flow_message as _send_vc_cat_flow
                            try:
                                await _send_vc_cat_flow(sender_id, _cat_flow_id, _vc_flow_token)
                            except Exception as _vc_cf_err:
                                logger.error(f"Category flow send failed after villa code entry for {sender_id}: {_vc_cf_err}")
                                await send_whatsapp_message(sender_id, "✅ Villa code saved! Type *Order Services* to browse available services.")
                        else:
                            await send_whatsapp_message(sender_id, "✅ Villa code saved! Type *Order Services* to browse available services.")
                    else:
                        await send_whatsapp_message(sender_id, f"✅ Welcome to *{villa_info.get('name', 'your villa')}*! You're all set. 🏡")
                        await starting_message(sender_id)
                    return
                else:
                    await send_whatsapp_message(sender_id, "❌ Sorry, there was an error saving your Villa Code. Please try again.")
                    return

            # ── Not a V-code → try villa name or location fuzzy match ────────────
            from app.services.menu_services import get_villa_code_by_name as _get_vc_by_name, get_all_villas as _get_all_villas
            _fuzzy_vc = await _get_vc_by_name(_vc_input)
            if _fuzzy_vc:
                villa_info = await get_villa_info_by_code(_fuzzy_vc)
                await save_user_villa_code(sender_id, _fuzzy_vc, source="manual_entry")
                await _del_vc_session(sender_id)
                if _vc_ctx == "start_booking" and await _try_resume_pending_booking(sender_id):
                    return
                if _vc_ctx == "order_services":
                    _cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                    if _cat_flow_id:
                        import uuid as _uuid_vc3
                        _vc3_flow_token = f"cat_{sender_id}_{_uuid_vc3.uuid4().hex[:8]}"
                        from app.services.whatsapp_flows_service import send_category_flow_message as _send_vc3_cat_flow
                        try:
                            await _send_vc3_cat_flow(sender_id, _cat_flow_id, _vc3_flow_token)
                        except Exception as _e3:
                            logger.error(f"Category flow send failed after name match for {sender_id}: {_e3}")
                            await send_whatsapp_message(sender_id, "✅ Villa found! Type *Order Services* to continue.")
                    else:
                        await send_whatsapp_message(sender_id, "✅ Villa found! Type *Order Services* to continue.")
                else:
                    await send_whatsapp_message(sender_id, f"✅ Welcome to *{(villa_info or {}).get('name', 'your villa')}*! 🏡")
                    await starting_message(sender_id)
                return

            # ── Try location/area match → show numbered villa list ────────────────
            _all_villas = await _get_all_villas()
            _input_lower = _vc_input.lower()
            _location_matches = [v for v in _all_villas if v.get("location", "").lower() and _input_lower in v["location"].lower()]
            if _location_matches:
                _list_lines = "\n".join(f"{i+1}. {v['name']}" for i, v in enumerate(_location_matches))
                await _save_vc_session(sender_id, {"ctx": _vc_ctx, "step": "awaiting_villa_choice", "villas": _location_matches, "ts": datetime.datetime.now().isoformat()})
                await send_whatsapp_message(
                    sender_id,
                    f"Villas in *{_vc_input.title()}*:\n\n"
                    f"{_list_lines}\n\n"
                    "Reply with the *number* of your villa to continue."
                )
                return

            # ── Nothing matched → proactively show all available locations ────────
            _all_locations = sorted(set(
                v["location"] for v in _all_villas if v.get("location", "").strip()
            ))
            if _all_locations:
                _loc_lines = "\n".join(f"{i+1}. {l}" for i, l in enumerate(_all_locations))
                await _save_vc_session(sender_id, {"ctx": _vc_ctx, "step": "awaiting_location_choice", "locations": _all_locations, "ts": datetime.datetime.now().isoformat()})
                await send_whatsapp_message(
                    sender_id,
                    "No problem! Which area is your villa in?\n\n"
                    f"{_loc_lines}\n\n"
                    "Reply with the *number* of your area.\n\n"
                    "Or type *Hi* to browse the menu without booking."
                )
            else:
                await _del_vc_session(sender_id)
                await send_whatsapp_message(
                    sender_id,
                    "To browse and book our services, we need to know which villa you're staying at "
                    "so we can show you the right options and pricing.\n\n"
                    "Please share your:\n"
                    "• *Villa code* (e.g. V1, V2) — on your welcome card or QR\n"
                    "• *Villa name* (e.g. Villa Manila)\n"
                    "• *Area* (e.g. Seminyak, Canggu)\n\n"
                    "Or type *Hi* to explore the general menu (Bali Handbook, Currency Converter, Voice Translator and more)."
                )
            return

        # Active Passport Submission Session — handled regardless of villa code
        if sender_id in passport_sessions:
            session = passport_sessions[sender_id]

            if message_text == "CANCEL":
                passport_sessions.pop(sender_id, None)
                await send_whatsapp_message(sender_id, "Passport submission cancelled.")
                await starting_message(sender_id)
                return

            if session["step"] == "awaiting_name":
                # PASSPORT-NAME-MINLEN-01 (2026-09-01, Clay live feedback):
                # the old `len(message_text) < 3` gate rejected genuinely
                # short real names (e.g. "Cy", "Jo"). Only guard against a
                # blank/whitespace-only reply — do not impose an arbitrary
                # minimum length on someone's actual name.
                if not message_text or not message_text.strip():
                    await send_whatsapp_message(sender_id, "Please enter your full name:")
                    return

                session["guest_name"] = message_text
                session["step"] = "awaiting_file"
                await send_whatsapp_message(
                    sender_id,
                    f"Thank you, *{message_text}*.\n\n"
                    "Now, please **upload a clear photo** or **PDF document** of your passport. 📸 📄"
                )
                return

            elif session["step"] == "awaiting_name_for_pending_media":
                # PASSPORT-DIRECT-ATTACH-NAME-01: the guest already attached
                # their photo before this session started (via the
                # pending_media_passport button flow) — the media_id is
                # already on the session, so unlike "awaiting_name" above,
                # answering this question uploads immediately instead of
                # asking for a second file.
                if not message_text or not message_text.strip():
                    await send_whatsapp_message(sender_id, "Please enter your full name:")
                    return

                await send_whatsapp_message(sender_id, "⏳ Processing your document, please wait...")
                success, msg = await process_whatsapp_passport(
                    sender_id, session["media_id"], session["villa_code"],
                    guest_name=message_text, customer_id=customer_id, guest_id=session.get("guest_id")
                )
                await send_whatsapp_message(sender_id, msg)
                if success:
                    passport_sessions.pop(sender_id, None)
                    await starting_message(sender_id)
                return

            elif session["step"] == "awaiting_file":
                media_id = None
                if "image" in message_payload:
                    media_id = message_payload["image"]["id"]
                elif "document" in message_payload:
                    media_id = message_payload["document"]["id"]

                if media_id:
                    villa_for_passport, _passport_guest_id, _passport_known_name = await _resolve_villa_and_guest_for_passport(sender_id)
                    await send_whatsapp_message(sender_id, "⏳ Processing your document, please wait...")
                    # PASSPORT-WA-NAME-01: the guest explicitly typed their
                    # name at the "awaiting_name" step above (session[
                    # "guest_name"]) — that is the authoritative source for
                    # THIS session. _passport_known_name (from an existing
                    # guest_profile) is only a fallback for the rare case the
                    # session value is somehow empty, never the reverse.
                    success, msg = await process_whatsapp_passport(
                        sender_id, media_id, villa_for_passport,
                        session.get("guest_name") or _passport_known_name,
                        customer_id=customer_id, guest_id=_passport_guest_id
                    )
                    await send_whatsapp_message(sender_id, msg)
                    if success:
                        passport_sessions.pop(sender_id, None)
                        await starting_message(sender_id)
                    return
                else:
                    await send_whatsapp_message(sender_id, "Please upload a passport image or PDF document. (Or type CANCEL to exit)")
                    return

        # New users tapping an interactive menu item need the same routing as
        # villa-code users. Track whether we should enter the shared handler.
        _run_shared_handler = bool(user_villa_code) or (
            not user_villa_code and (selected_id or serviceitems_text or message_text)
        )

        if _run_shared_handler:

            # ── Universal session escape + auto-expiry ───────────────────────────
            # Escape words always clear ALL active sessions and show the main menu,
            # so users are never permanently stuck in issue / feedback / language mode.
            _ESCAPE_WORDS = {
                "hi", "hello", "hey", "menu", "start", "back", "main menu",
                "home", "cancel", "reset", "restart", "begin", "help",
                "good morning", "good afternoon", "good evening",
            }
            _SESSION_TTL = datetime.timedelta(minutes=30)
            _now_ts = datetime.datetime.now()

            # Auto-expire issue sessions older than 30 min
            if sender_id in issue_reporting_sessions:
                _iss_ts = issue_reporting_sessions[sender_id].get("timestamp", _now_ts)
                if (_now_ts - _iss_ts) > _SESSION_TTL:
                    issue_reporting_sessions.pop(sender_id, None)
                    logger.info(f"Auto-expired stale issue session for {sender_id}")

            if sender_id in amenity_wa_sessions:
                _ami_ts = amenity_wa_sessions[sender_id].get("timestamp", _now_ts)
                if (_now_ts - _ami_ts) > _SESSION_TTL:
                    amenity_wa_sessions.pop(sender_id, None)
                    logger.info(f"Auto-expired stale amenity WA session for {sender_id}")

            if sender_id in dnp_wa_sessions:
                _dnp_ts = dnp_wa_sessions[sender_id].get("timestamp", _now_ts)
                if (_now_ts - _dnp_ts) > _SESSION_TTL:
                    dnp_wa_sessions.pop(sender_id, None)
                    logger.info(f"Auto-expired stale DNP session for {sender_id}")

            _vcs_doc = await villa_code_sessions_collection.find_one({"sender_id": sender_id})
            if _vcs_doc:
                _vcs_updated = _vcs_doc.get("updated_at", _now_ts)
                if isinstance(_vcs_updated, str):
                    try:
                        _vcs_updated = datetime.datetime.fromisoformat(_vcs_updated)
                    except Exception:
                        _vcs_updated = _now_ts
                if (_now_ts - _vcs_updated) > _SESSION_TTL:
                    await _del_vc_session(sender_id)
                    logger.info(f"Auto-expired stale villa_code session for {sender_id}")

            if message_text and message_text.strip().lower() in _ESCAPE_WORDS:
                issue_reporting_sessions.pop(sender_id, None)
                onboarding_sessions.pop(sender_id, None)
                persistent_mode_sessions.pop(sender_id, None)
                feedback_sessions.pop(sender_id, None)
                language_lesson_sessions.pop(sender_id, None)
                amenity_wa_sessions.pop(sender_id, None)
                dnp_wa_sessions.pop(sender_id, None)
                await _delete_booking_session(sender_id)
                await starting_message(sender_id)
                return
            # ────────────────────────────────────────────────────────────────────

            # ── Post-arrival onboarding: collect check-in date + stay duration ────
            if sender_id in onboarding_sessions and message_text:
                ob = onboarding_sessions[sender_id]
                ob_step = ob.get("step")

                if ob_step == "awaiting_name":
                    guest_name = message_text.strip()
                    if len(guest_name) < 2:
                        await send_whatsapp_message(
                            sender_id,
                            "Please reply with your full name, e.g. *John Smith* 🙏"
                        )
                        return
                    onboarding_sessions[sender_id]["full_name"] = guest_name
                    onboarding_sessions[sender_id]["step"] = "awaiting_checkin_date"
                    # Update customer record with name immediately
                    await customer_collection.update_one(
                        {"phone": sender_id},
                        {"$set": {"name": guest_name}}
                    )
                    await send_whatsapp_message(
                        sender_id,
                        f"🗓️ Great to meet you, *{guest_name.split()[0]}*!\n\n"
                        "So I can send you helpful tips and reminders at the right time — "
                        "*what date did you check in?*\n\n"
                        "Just reply with the date, e.g. *April 2* or *2/4*"
                    )
                    return

                if ob_step == "awaiting_checkin_date":
                    parsed_dt = _parse_checkin_reply(message_text)
                    if parsed_dt is None:
                        await send_whatsapp_message(
                            sender_id,
                            "🗓️ I couldn't quite catch that date. Could you try again?\n\n"
                            "Examples: *April 2*, *2/4*, *today*, *tomorrow*"
                        )
                        return
                    onboarding_sessions[sender_id]["checkin_date"] = parsed_dt
                    onboarding_sessions[sender_id]["step"] = "awaiting_duration"
                    guest_name = ob.get("full_name", "")
                    first_name = guest_name.split()[0] if guest_name else ""
                    await send_whatsapp_message(
                        sender_id,
                        f"✅ Got it — *{parsed_dt.strftime('%B %d')}*!\n\n"
                        "And how many nights are you staying? Just reply with a number, e.g. *5*"
                    )
                    return

                if ob_step == "awaiting_duration":
                    import re as _re
                    nights_match = _re.search(r'\d+', message_text)
                    if not nights_match:
                        await send_whatsapp_message(
                            sender_id,
                            "Just reply with a number — e.g. *5* for 5 nights 🙏"
                        )
                        return
                    nights = int(nights_match.group())
                    checkin_date = ob.get("checkin_date") or datetime.datetime.now()
                    checkout_date = checkin_date + datetime.timedelta(days=nights)
                    villa_code_ob = ob.get("villa_code", "")
                    full_name = ob.get("full_name", "")

                    # Persist precise dates into the checkin record
                    await checkin_collection.update_one(
                        {"sender_id": sender_id, "status": "active"},
                        {"$set": {
                            "checkin_date": checkin_date,
                            "checkout_date": checkout_date,
                            "stay_nights": nights,
                            "full_name": full_name,
                        }}
                    )

                    # Create / update guest_profile so this WhatsApp guest appears in dashboard
                    try:
                        from app.db.session import guest_profile_collection as _gpc
                        import re as _re2
                        _phone_digits = _re2.sub(r"[\s\-\(\)\.\+]", "", sender_id)
                        _phone_digits = _re2.sub(r"^00", "", _phone_digits)
                        _existing_gp = await _gpc.find_one({"phone_number": _phone_digits})
                        _villa_info = await get_villa_info_by_code(villa_code_ob)
                        _villa_name = (_villa_info or {}).get("name", "")
                        _location = (_villa_info or {}).get("location", "Bali")
                        _now = datetime.datetime.utcnow()
                        _gp_data = {
                            "full_name": full_name,
                            "phone_number": _phone_digits,
                            "villa_code": villa_code_ob,
                            "villa_name": _villa_name,
                            "location_zone": _location,
                            "check_in_date": checkin_date.strftime("%Y-%m-%d"),
                            "check_out_date": checkout_date.strftime("%Y-%m-%d"),
                            "source": "whatsapp",
                            "last_active_at": _now,
                        }
                        if _existing_gp:
                            await _gpc.update_one({"phone_number": _phone_digits}, {"$set": _gp_data})
                            _guest_id = _existing_gp.get("guest_id")
                        else:
                            import uuid as _uuid
                            _guest_id = str(_uuid.uuid4())
                            _gp_data["guest_id"] = _guest_id
                            _gp_data["created_at"] = _now
                            await _gpc.insert_one(_gp_data)
                        # Back-link guest_id into checkin record
                        await checkin_collection.update_one(
                            {"sender_id": sender_id, "status": "active"},
                            {"$set": {"guest_id": _guest_id}}
                        )
                        # Back-link guest_id into customer record
                        await customer_collection.update_one(
                            {"phone": sender_id},
                            {"$set": {"guest_id": _guest_id, "name": full_name}}
                        )
                        logger.info(f"Guest profile upserted for WA guest {sender_id[-4:]}: guest_id={_guest_id}")
                    except Exception as _gp_err:
                        logger.warning(f"Guest profile upsert failed (non-fatal): {_gp_err}")

                    onboarding_sessions.pop(sender_id, None)
                    logger.info(
                        f"Onboarding complete for {sender_id}: "
                        f"checkin={checkin_date.date()}, checkout={checkout_date.date()}, nights={nights}"
                    )
                    first_name = full_name.split()[0] if full_name else "there"
                    await send_whatsapp_message(
                        sender_id,
                        f"🎉 Perfect, *{first_name}*! *{nights} night{'s' if nights != 1 else ''}* from "
                        f"*{checkin_date.strftime('%B %d')}* to *{checkout_date.strftime('%B %d')}*.\n\n"
                        "I'll send you tips and reminders at the right times. Enjoy your stay! 🌴"
                    )
                    await starting_message(sender_id)
                    return
            # ─────────────────────────────────────────────────────────────────────

            # ── Booking form: text input handler (name / phone / date) ───────────
            if message_text:
                bk_session = await _get_booking_session(sender_id)
                bk_step = bk_session.get("step") if bk_session else None

                if bk_step == "awaiting_name":
                    name_val = message_text.strip()
                    if len(name_val) < 2:
                        await send_whatsapp_message(sender_id, "⚠️ Please enter your full name (at least 2 characters).")
                        return
                    bk_session["name"] = name_val
                    bk_session["step"] = "awaiting_phone"
                    await _save_booking_session(sender_id, bk_session)
                    card = _build_form_card(bk_session.get("service_name", ""), bk_session.get("price", ""), bk_session)
                    await send_whatsapp_message(sender_id, card)
                    return

                if bk_step == "awaiting_phone":
                    phone_val = re.sub(r"\s+", "", message_text.strip())
                    if len(phone_val) < 7:
                        await send_whatsapp_message(sender_id, "⚠️ Please enter a valid phone number including country code (e.g. +628123456789).")
                        return
                    bk_session["phone"] = phone_val
                    bk_session["step"] = "awaiting_date"
                    await _save_booking_session(sender_id, bk_session)
                    card = _build_form_card(bk_session.get("service_name", ""), bk_session.get("price", ""), bk_session)
                    await send_whatsapp_message(sender_id, card)
                    return

                if bk_step == "awaiting_date":
                    raw_date = message_text.strip()
                    try:
                        import dateutil.parser as _dp
                        parsed_date = _dp.parse(raw_date, dayfirst=True)
                        if parsed_date.date() < datetime.datetime.now().date():
                            await send_whatsapp_message(sender_id, "⚠️ That date is in the past. Please enter a future date (e.g. *28/03/2026*).")
                            return
                        date_str = parsed_date.strftime("%d/%m/%Y")
                    except Exception:
                        await send_whatsapp_message(sender_id, "⚠️ I couldn't understand that date. Please try again (e.g. *28/03/2026*).")
                        return
                    bk_session["date"] = date_str
                    bk_session["step"] = "awaiting_time"
                    await _save_booking_session(sender_id, bk_session)
                    await _send_time_buttons(sender_id)
                    return
            # ──────────────────────────────────────────────────────────────────

            # Task 22: Active Issue Reporting Session
            if sender_id in issue_reporting_sessions:
                session = issue_reporting_sessions[sender_id]

                if message_text == "CANCEL":
                    issue_reporting_sessions.pop(sender_id, None)
                    await send_whatsapp_message(sender_id, "Issue reporting cancelled.")
                    await starting_message(sender_id)
                    return

                step = session.get("step", "awaiting_description")

                # ── Step 1: Collect initial description ──────────────────────────
                if step == "awaiting_description":
                    description = message_text or "Maintenance issue reported"
                    if "image" in message_payload:
                        description = message_payload["image"].get("caption") or f"📸 Photo submitted by Guest {sender_id[-4:]}"
                        session["initial_media_id"] = message_payload["image"]["id"]
                        session["initial_media_type"] = "image"
                    elif "audio" in message_payload:
                        # message_text already contains the Whisper transcript (set at top of process_message)
                        description = message_text or f"🎙️ Voice note from Guest {sender_id[-4:]}"
                        session["initial_media_id"] = message_payload["audio"]["id"]
                        session["initial_media_type"] = "voice_note"

                    session["description"] = description
                    session["step"] = "awaiting_media_or_done"
                    issue_reporting_sessions[sender_id] = session
                    await send_whatsapp_message(
                        sender_id,
                        "📝 *Description noted!*\n\n"
                        "You can now add a *photo* 📸 or *voice note* 🎤 to support your report.\n"
                        "_(Type *DONE* to submit now without media)_"
                    )
                    return

                # ── Step 2: Optional media or DONE, then submit ───────────────────
                description = session.get("description", "Maintenance issue reported")
                attachment_url = None
                media_id = None
                media_type = "text"

                _done_words = {"done", "skip", "submit", "send", "ok", "okay", "no", "none"}
                if message_text and message_text.strip().lower() in _done_words:
                    # Submit with any media that was already stored from step 1
                    media_id = session.get("initial_media_id")
                    media_type = session.get("initial_media_type", "text")
                elif "image" in message_payload:
                    media_id = message_payload["image"]["id"]
                    media_type = "image"
                    extra_caption = message_payload["image"].get("caption")
                    if extra_caption:
                        description = f"{description} | {extra_caption}"
                elif "audio" in message_payload:
                    media_id = message_payload["audio"]["id"]
                    media_type = "voice_note"
                elif message_text:
                    # Additional text — append and keep session open for media
                    session["description"] = f"{description}\n{message_text}"
                    issue_reporting_sessions[sender_id] = session
                    await send_whatsapp_message(
                        sender_id,
                        "✅ Added to your report. Send a *photo* 📸 or *voice note* 🎤, or type *DONE* to submit."
                    )
                    return

                # Process and upload media if present
                if media_id:
                    success, attachment_url, transcript = await process_whatsapp_issue(
                        sender_id, media_id, user_villa_code, description, media_type,
                        customer_id=customer_id
                    )
                    if not success:
                        await send_whatsapp_message(sender_id, "⚠️ Sorry, there was an issue processing your media. Type *DONE* to submit without it.")
                        return
                    if transcript:
                        description = f"{session.get('description', '')} {transcript}".strip()
                else:
                    # Log plain text issue (no media at all)
                    issue_data = {
                        "sender_id": sender_id,
                        "customer_id": customer_id,
                        "villa_code": user_villa_code,
                        "description": description,
                        "media_type": "text",
                        "status": "open",
                        "source": "whatsapp",
                        "timestamp": datetime.datetime.now()
                    }
                    await issue_collection.insert_one(issue_data)

                # Notify Villa Manager
                # VM-MAINT-SHARED-LOOKUP-01 (2026-09-04): this block used to
                # duplicate the villa_profiles-priority-over-sheet lookup
                # inline, with the lookup itself unwrapped by any try/except
                # — a db/sheet failure here would propagate uncaught and
                # could interrupt the guest's own "Issue Received"
                # confirmation below. Replaced with the shared, tested
                # get_villa_whatsapp_by_code() (already used by the amenity
                # and passport-submission WhatsApp paths) so this stays in
                # sync with any future VM-number resolution change, and the
                # lookup itself is now non-fatal like every other VM-notify
                # call site in this file.
                villa_info = None
                clean_mgr = None
                try:
                    villa_info = await get_villa_info_by_code(user_villa_code)
                    clean_mgr = await get_villa_whatsapp_by_code(user_villa_code)
                except Exception as _lookup_err:
                    logger.warning(f"Issue submitted for {user_villa_code} — VM lookup failed (non-fatal): {_lookup_err}")

                if clean_mgr:
                    media_info = f"\n🖼️ *Attachment:* [View Media]({attachment_url})" if attachment_url else ""
                    mgr_msg = (
                        f"🚨 *NEW ISSUE REPORTED!*\n\n"
                        f"Villa: *{(villa_info or {}).get('name', user_villa_code)}*\n"
                        f"Guest Contact: {sender_id}\n"
                        f"Issue: {description}{media_info}\n\n"
                        f"Please check the dashboard for details."
                    )
                    try:
                        await send_whatsapp_message(clean_mgr, mgr_msg)
                        logger.info(f"Villa manager notified at {clean_mgr} for issue from {sender_id} in {user_villa_code}")
                    except Exception as e:
                        logger.error(f"Failed to notify manager {clean_mgr} for {user_villa_code}: {e}")
                else:
                    logger.warning(f"Issue submitted for {user_villa_code} but no manager number found — villa_info={villa_info is not None}")

                issue_reporting_sessions.pop(sender_id, None)
                await send_whatsapp_message(
                    sender_id,
                    "✅ *Issue Received*\n\n"
                    "Thank you for reporting this. Our maintenance team and the villa manager have been notified and will address this as soon as possible."
                )
                return

            # ETA capture — guest replies to pre-arrival message with their arrival time
            if message_text and len(message_text.strip()) <= 40:
                guest_reg = await db["guest_registrations"].find_one(
                    {"sender_id": sender_id, "status": "expected", "awaiting_eta": True}
                )
                if guest_reg:
                    await db["guest_registrations"].update_one(
                        {"_id": guest_reg["_id"]},
                        {"$set": {"eta": message_text.strip(), "awaiting_eta": False}}
                    )
                    await send_whatsapp_message(
                        sender_id,
                        f"✅ Got it! We've noted your arrival time as *{message_text.strip()}*. See you soon! 🌴"
                    )
                    return

            # Feedback comment collection (awaiting text after rating)
            if sender_id in feedback_sessions:
                session = feedback_sessions[sender_id]
                comment = message_text.strip() if message_text else ""
                await feedback_collection.update_one(
                    {"_id": session["feedback_id"]},
                    {"$set": {"comment": comment}}
                )
                feedback_sessions.pop(sender_id, None)
                await send_whatsapp_message(sender_id, "Thank you for your comment! We really appreciate it. 🙏")
                return

            # Text-based feedback detector — catches messages like "my feedback is..." or "service was great"
            _feedback_keywords = ["my feedback", "feedback:", "feedback is", "i want to give feedback",
                                   "i'd like to give feedback", "overall experience", "service was",
                                   "stay was", "experience was", "review:", "my review", "i want to review"]
            if message_text and any(kw in message_text.lower() for kw in _feedback_keywords):
                await feedback_collection.insert_one({
                    "sender_id": sender_id,
                    "customer_id": customer_id,
                    "villa_code": user_villa_code,
                    "rating": None,
                    "comment": message_text.strip(),
                    "timestamp": datetime.datetime.now()
                })
                # Let the AI respond naturally — don't return here so the AI reply still goes out

            # Task 22/23: Feedback Collection Detector
            # Accept ratings from any guest who has a checkin record (not just after automation)
            if message_text and message_text.strip() in ["1", "2", "3", "4", "5"]:
                checkin = await checkin_collection.find_one(
                    {"sender_id": sender_id},
                    sort=[("checkin_time", -1)]
                )
                if checkin:
                    rating = int(message_text.strip())
                    result = await feedback_collection.insert_one({
                        "sender_id": sender_id,
                        "customer_id": customer_id,
                        "villa_code": user_villa_code,
                        "rating": rating,
                        "comment": "",
                        "timestamp": datetime.datetime.now()
                    })
                    # Open a session to optionally collect a text comment
                    feedback_sessions[sender_id] = {"feedback_id": result.inserted_id}

                    # Fetch villa profile once — used for review_link (positive) and manager_phone (negative)
                    rich_profile = await db["villa_profiles"].find_one({"villa_code": user_villa_code})

                    if rating >= 4:
                        # Task 23: Positive Feedback — use villa-specific review link if configured
                        review_link = (rich_profile or {}).get("review_link") or "https://g.page/r/easybali/review"
                        review_msg = (
                            "💖 *Thank you so much!* We are thrilled you had a great stay.\n\n"
                            "Feel free to share any additional comments, or type *skip* to continue.\n\n"
                            "You can also leave a review for other travelers:\n"
                            f"👉 [Leave a Review]({review_link})\n\n"
                            "Have a safe flight! ✈️"
                        )
                        await send_whatsapp_message(sender_id, review_msg)
                    else:
                        # Task 23: Critical Feedback - Notify Manager
                        sorry_msg = (
                            "😔 *We are truly sorry to hear that.*\n\n"
                            "We strive for 5-star experiences and it seems we missed the mark. Our manager has been notified and we will review your feedback to improve.\n\n"
                            "Was there something specific we could have done better? Please share your comments:"
                        )
                        await send_whatsapp_message(sender_id, sorry_msg)

                        # Notify Manager (rich_profile already fetched above)
                        _low_villa_info = await get_villa_info_by_code(user_villa_code)
                        mgr_num = (rich_profile or {}).get("manager_phone") or (_low_villa_info or {}).get("manager_number")
                        if mgr_num:
                            _low_villa_name = (_low_villa_info or {}).get("name") or user_villa_code
                            alert = (
                                f"⚠️ *Low Rating Alert!*\n\n"
                                f"Villa: *{_low_villa_name}*\n"
                                f"Guest ID: `...{sender_id[-4:]}`\n"
                                f"Rating: {rating}/5 Stars\n\n"
                                f"Please follow up to address any grievances."
                            )
                            await send_whatsapp_message("".join(filter(str.isdigit, str(mgr_num))), alert)
                    return

            if message_text and message_text.lower() in ["hello", "hi", "hey", "good morning", "good afternoon", "good evening", "hey there", "hi there", "hello there", "howdy", "what's up?", "how are you?", "how's it going?", "yo", "greetings", "bonjour"]:
                await starting_message(sender_id)
                return

            # ── Virtual Orientation on-demand ─────────────────────────────────────────
            # Guest can request the orientation video/guide at any time after check-in.
            _ORIENTATION_KEYWORDS = {
                "orientation", "virtual tour", "villa tour", "villa orientation",
                "show me around", "tour", "guide", "house guide", "villa guide"
            }
            if message_text and message_text.strip().lower() in _ORIENTATION_KEYWORDS:
                _vi = await get_villa_info_by_code(user_villa_code or "")
                _rp = await db["villa_profiles"].find_one({"villa_code": user_villa_code}) if user_villa_code else None
                _orient_url = (_rp or {}).get("orientation_link", "")
                _map_url = (_rp or {}).get("maps_link") or (_vi or {}).get("map_link", "")
                if _orient_url:
                    await send_whatsapp_interactive_link_with_text(
                        sender_id,
                        _orient_url,
                        button_text="Virtual Orientation",
                        body_text="🎬 Here's your villa's virtual orientation guide. Tap to explore!"
                    )
                    if _map_url:
                        await send_whatsapp_interactive_link_with_text(
                            sender_id,
                            _map_url,
                            button_text="View on Maps",
                            body_text="📍 And here's the villa location on Google Maps."
                        )
                else:
                    await send_whatsapp_message(
                        sender_id,
                        "🎬 The virtual orientation for your villa hasn't been set up yet.\n\n"
                        "Please ask your villa manager or reception desk — they'll be happy to assist!"
                    )
                return
            # ─────────────────────────────────────────────────────────────────────────

            # Task 22: Issue Reporting Detection
            # NOTE: "help me" was removed 2026-07-11 — it is far too broad ("help me
            # arrange a massage", "help me book…") and misrouted booking/service
            # requests into maintenance reporting. Keep only issue-specific words.
            if message_text and any(word in message_text.lower() for word in ["issue", "problem", "broken", "not working", "complain", "maintenance", "leak", "not turning on"]):
                # Start a 2-step issue session. For voice notes, pre-fill the description from the transcript.
                _pre_desc = None
                _pre_media_id = None
                _pre_media_type = None
                if "audio" in message_payload:
                    _pre_desc = f"🎙️ (Voice Note): {message_text}"
                    _pre_media_id = message_payload["audio"]["id"]
                    _pre_media_type = "voice_note"

                issue_reporting_sessions[sender_id] = {
                    "step": "awaiting_media_or_done" if _pre_desc else "awaiting_description",
                    "description": _pre_desc,
                    "initial_media_id": _pre_media_id,
                    "initial_media_type": _pre_media_type,
                    "timestamp": datetime.datetime.now()
                }
                if _pre_desc:
                    await send_whatsapp_message(
                        sender_id,
                        "📝 *Description noted!*\n\n"
                        "You can now add a *photo* 📸 or *voice note* 🎤 to support your report.\n"
                        "_(Type *DONE* to submit now without media)_"
                    )
                else:
                    await send_whatsapp_message(
                        sender_id,
                        "⚠️ *Issue Reporting Mode*\n\n"
                        "I'm sorry to hear you're experiencing an issue. Please describe the problem in detail.\n\n"
                        "You can also send a *Photo* 📸 or *Voice Note* 🎤 to help us understand the situation better.\n"
                        "_(Type *CANCEL* to exit)_"
                    )
                return

            if message_text and sender_id in persistent_mode_sessions and not serviceitems_text and not category_text and not selected_id:
                mode = persistent_mode_sessions.get(sender_id)
                if mode and mode in PERSISTENT_MODE_CHAT_TYPES:
                    chat_type = PERSISTENT_MODE_CHAT_TYPES[mode]
                    _msg = message_text
                    if _msg == "__THIS_WEEK_EVENTS__":
                        import datetime as _dt_mod
                        _today = _dt_mod.datetime.now()
                        _wstart = _today - _dt_mod.timedelta(days=_today.weekday())
                        _wend = _wstart + _dt_mod.timedelta(days=6)
                        _dr = f"{_wstart.strftime('%d %B %Y')} to {_wend.strftime('%d %B %Y')}"
                        _msg = f"What events are happening this week ({_dr}) in Bali?"
                    data = await _whatsapp_ai_chat(sender_id, _msg, chat_type)
                    if data:
                        await send_whatsapp_message(sender_id, data)
                        # ── persistent-mode catalog menu (additive, WCR-10 companion) ────────
                        # whatsapp_response() — where WCR-10 lives — is bypassed in this path.
                        # If the message contains a catalog phrase (shisha, hookah, massage …)
                        # AND the guest has a villa code, send the Category Flow so the guest
                        # can tap straight into the booking flow without typing anything else.
                        if user_villa_code:
                            try:
                                from app.services.whatsapp_ai_prompt import _CATALOG_PHRASES_WA as _CPW
                                _pm_q = message_text.lower() if message_text else ""
                                if any(_cp in _pm_q for _cp in _CPW):
                                    _pm_flow_id = "1465038141489393"
                                    import uuid as _upm
                                    _pm_tok = f"cat_{sender_id}_{_upm.uuid4().hex[:8]}"
                                    from app.services.whatsapp_flows_service import send_category_flow_message as _spm_cat
                                    try:
                                        await _spm_cat(sender_id, _pm_flow_id, _pm_tok)
                                    except Exception as _pm_cf_err:
                                        logger.error(f"[PM cat flow] non-fatal: {_pm_cf_err}")
                            except Exception as _pm_cw_err:
                                logger.error(f"[PM catalog check] non-fatal: {_pm_cw_err}")
                        # ─────────────────────────────────────────────────────────────────────
                    # None means either booking flow was sent or error — either way, no extra message needed
                return

            # ── What To Do Today? → show 4 mood-based options (must check BEFORE PERSISTENT_MODE_CHAT_TYPES) ──
            if selected_id == "order_services":
                logger.critical(f"ORDER_SERVICES_SELECTED sender={sender_id} selected_id={selected_id}")
                _os_existing_vc = await get_user_villa_code(sender_id)
                logger.critical(f"ORDER_SERVICES_VC_CHECK sender={sender_id} villa_code={repr(_os_existing_vc)}")
                if not _os_existing_vc:
                    await _save_vc_session(sender_id, "order_services")
                    await send_whatsapp_message(
                        sender_id,
                        "To show you the right services and prices, we need to know your villa.\n\n"
                        "Do you have your *villa code*? It's a short code like *V1* or *V2* — "
                        "you'll find it on your welcome card or villa QR sticker.\n\n"
                        "• *Type your villa code* — e.g. V1\n"
                        "• Reply *no* if you don't have it and we'll help you find your villa"
                    )
                    return
                # Returning guest — launch Category Flow
                _cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                if _cat_flow_id:
                    import uuid as _uuid_os
                    _os_flow_token = f"cat_{sender_id}_{_uuid_os.uuid4().hex[:8]}"
                    from app.services.whatsapp_flows_service import send_category_flow_message as _send_os_cat_flow
                    try:
                        await _send_os_cat_flow(sender_id, _cat_flow_id, _os_flow_token)
                    except Exception as _os_cf_err:
                        logger.error(f"Category flow send failed for {sender_id}: {_os_cf_err}")
                return

            if selected_id == "what_to_do_today":
                wtd_rows = [
                    {"id": "wtd_instagram",    "title": "📸 Instagram Photos",   "description": "I want nice photos for my Instagram"},
                    {"id": "wtd_quality_time", "title": "👨‍👩‍👧 Quality Time",      "description": "Spend time with friends or family"},
                    {"id": "wtd_local",        "title": "🌺 Local Experience",   "description": "Try something authentically local"},
                    {"id": "wtd_crazy",        "title": "🤪 Something Crazy",    "description": "Do something adventurous!"},
                ]
                card_data = {
                    "main_title": "What To Do Today? 🌴",
                    "main_description": "What are you in the mood for?",
                    "data": wtd_rows,
                }
                await send_whatsapp_menu_list_message(sender_id, card_data)
                return

            if selected_id in PERSISTENT_MODE_CHAT_TYPES:
                chat_type = PERSISTENT_MODE_CHAT_TYPES[selected_id]
                persistent_mode_sessions[sender_id] = selected_id
                kickoff = _KICKOFF_MESSAGES.get(selected_id, "Greet the guest warmly and ask how you can help them today in Bali.")
                data = await _whatsapp_ai_chat(sender_id, kickoff, chat_type)
                if data:
                    await send_whatsapp_message(sender_id, data)
                # None means booking flow sent or error — no extra message needed
                return
            
            elif selected_id == "language_lesson":
                language_lesson_sessions[sender_id] = {"mode": "structured", "word_index": 0, "timestamp": datetime.datetime.now()}
                await language_starting_message(sender_id)
                return

            if sender_id in language_lesson_sessions:
                if message_text:
                    ai_response = await language_lesson_response(user_id=sender_id, query=message_text)
                    await send_whatsapp_message(sender_id, ai_response)
                    return
                else:
                    language_lesson_sessions.pop(sender_id, None)

            # Your existing menu mapping logic
            menu_mapping = {
                "Menu": "Main Menu",
                "menu": "Main Menu",
                "main menu": "Main Menu",
                "category": "Category",
                "show category": "Category",
                "show menu": "Main Menu",
                "🔙 Main Menu": "Main Menu"
            }

            if category_text in menu_mapping:
                api_url = f"{settings.BASE_URL}/main_design"
                menu_data = await fetch_menu_data(api_url, "Main Menu")
                if menu_data:
                    if isinstance(menu_data, list):
                        menu_data = {"data": menu_data}
                    await send_whatsapp_menu_list_message(recipient_id=sender_id, card_data=menu_data)
                return

            elif serviceitems_text:
                # ── 1. Hard-wired special flows ──────────────────────────────
                if serviceitems_text == "Order Services":
                    # ── Resolve villa code first ──────────────────────────────────
                    # Returning guests (villa already in DB) go straight to the flow.
                    # New guests are asked for their villa code before the flow opens
                    # so they are registered before any booking is created.
                    _os_existing_vc = await get_user_villa_code(sender_id)
                    if not _os_existing_vc:
                        # New user — ask for villa code first.
                        # If they reply with a code → validated + saved by the _vc_session handler below.
                        # If they reply "no" → _vc_session handler sends the Venue Setup Flow.
                        await _save_vc_session(sender_id, "order_services")
                        await send_whatsapp_message(
                            sender_id,
                            "To show you the right services and prices, we need to know your villa.\n\n"
                            "Do you have your *villa code*? It's a short code like *V1* or *V2* — "
                            "you'll find it on your welcome card or villa QR sticker.\n\n"
                            "• *Type your villa code* — e.g. V1\n"
                            "• Reply *no* if you don't have it and we'll help you find your villa"
                        )
                        return

                    # Returning guest — launch Category Flow directly at FIRST_SCREEN
                    _cat_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                    if _cat_flow_id:
                        import uuid as _uuid
                        _flow_token = f"cat_{sender_id}_{_uuid.uuid4().hex[:8]}"
                        from app.services.whatsapp_flows_service import send_category_flow_message as _send_cat_flow
                        try:
                            await _send_cat_flow(sender_id, _cat_flow_id, _flow_token)
                        except Exception as _cf_err:
                            logger.error(f"Category flow send failed for {sender_id}: {_cf_err}")
                            # Fall back to text list
                            _os_data = await fetch_menu_design("Order Services", villa_code=_os_existing_vc)
                            if _os_data:
                                items_list = _os_data if isinstance(_os_data, list) else _os_data.get("items", [])
                                _os_id_map = {}
                                _os_rows = []
                                for _i, _item in enumerate(items_list):
                                    _cat = _item.get("category", "")
                                    if not _cat:
                                        continue
                                    _rid = f"os_cat_{_i}"
                                    _os_id_map[_rid] = _cat
                                    _desc = str(_item.get("description", "Tap to explore"))
                                    _os_rows.append({"id": _rid, "title": _cat[:24], "description": _desc[:69]})
                                sheet_nav_sessions[sender_id] = {"main_menu": "Order Services", "id_map": _os_id_map}
                                _card = {"main_title": "Order Services", "main_description": "Choose a service category:", "data": _os_rows}
                                await send_whatsapp_menu_list_message(sender_id, _card)
                    else:
                        # Flow not configured — use existing text list
                        _os_data = await fetch_menu_design("Order Services", villa_code=_os_existing_vc)
                        if _os_data:
                            items_list = _os_data if isinstance(_os_data, list) else _os_data.get("items", [])
                            _os_id_map = {}
                            _os_rows = []
                            for _i, _item in enumerate(items_list):
                                _cat = _item.get("category", "")
                                if not _cat:
                                    continue
                                _rid = f"os_cat_{_i}"
                                _os_id_map[_rid] = _cat
                                _desc = str(_item.get("description", "Tap to explore"))
                                _os_rows.append({"id": _rid, "title": _cat[:24], "description": _desc[:69]})
                            sheet_nav_sessions[sender_id] = {"main_menu": "Order Services", "id_map": _os_id_map}
                            _card = {"main_title": "Order Services", "main_description": "Choose a service category:", "data": _os_rows}
                            await send_whatsapp_menu_list_message(sender_id, _card)
                        else:
                            await send_whatsapp_message(sender_id, "Please choose a category from Order Services below 👇")
                    return

                if serviceitems_text == "Voice Translator":
                    persistent_mode_sessions[sender_id] = "voice_translator"
                    data = await _whatsapp_ai_chat(sender_id, "hi", "voice-translator")
                    if data:
                        await send_whatsapp_message(sender_id, data)
                    return

                if serviceitems_text == "Language Lesson":
                    language_lesson_sessions[sender_id] = {"mode": "structured", "word_index": 0, "timestamp": datetime.datetime.now()}
                    await language_starting_message(sender_id)
                    return

                # ── What To Do Today? → show 4 mood-based options ────────────────
                if serviceitems_text == "What To Do Today?":
                    wtd_rows = [
                        {"id": "wtd_instagram",    "title": "📸 Instagram Photos",   "description": "I want nice photos for my Instagram"},
                        {"id": "wtd_quality_time", "title": "👨‍👩‍👧 Quality Time",      "description": "Spend time with friends or family"},
                        {"id": "wtd_local",        "title": "🌺 Local Experience",   "description": "Try something authentically local"},
                        {"id": "wtd_crazy",        "title": "🤪 Something Crazy",    "description": "Do something adventurous!"},
                    ]
                    card_data = {
                        "main_title": "What To Do Today? 🌴",
                        "main_description": "What are you in the mood for?",
                        "data": wtd_rows,
                    }
                    await send_whatsapp_menu_list_message(sender_id, card_data)
                    return

                # ── 1c. Sheet-driven menus (Menu Structure tab) ──────────────────
                _sheet_main_menu = _SHEET_DRIVEN_MENUS.get(serviceitems_text)
                if _sheet_main_menu:
                    cats = await get_sheet_menu_categories(_sheet_main_menu)
                    if cats:
                        id_map = {}
                        _use_buttons = serviceitems_text in _BUTTON_NAV_MENUS and len(cats) <= 3
                        if _use_buttons:
                            btns = []
                            for i, c in enumerate(cats):
                                cid = f"shbcat_{i}"
                                id_map[cid] = c["category"]
                                btns.append({"id": cid, "title": c["category"][:20]})
                            sheet_nav_sessions[sender_id] = {
                                "main_menu": _sheet_main_menu,
                                "id_map": id_map,
                                "use_buttons": True,
                            }
                            await _send_nav_buttons(sender_id, f"*{serviceitems_text}*\nWhat would you like to explore?", btns)
                        else:
                            rows = []
                            for i, c in enumerate(cats):
                                cid = f"shcat_{i}"
                                id_map[cid] = c["category"]
                                rows.append({
                                    "id": cid,
                                    "title": c["category"][:24],
                                    "description": "Tap to explore",
                                })
                            sheet_nav_sessions[sender_id] = {
                                "main_menu": _sheet_main_menu,
                                "id_map": id_map,
                            }
                            card_data = {
                                "main_title": "Bali Handbook",
                                "main_description": "What would you like to explore?",
                                "data": rows,
                            }
                            await send_whatsapp_menu_list_message(sender_id, card_data)
                        return
                    # No categories in sheet — fall through to existing logic

                # ── 2. Sub-menu parents → show sheet list, AI fallback if empty ──
                if serviceitems_text in _SUBMENU_PARENTS:
                    main_design = await fetch_menu_design(serviceitems_text)
                    if main_design and main_design.get("items"):
                        await send_whatsapp_menu_list_message(sender_id, main_design)
                        return
                    # Sheet has no items yet — fall through to AI chat

                # ── 3. Sheet Button URL lookup (title match, any item) ────────
                _btn_url = None
                try:
                    from app.services.menu_services import cache as _menu_cache
                    _df = _menu_cache.get("main_menu_design")
                    if _df is not None:
                        _match = _df[_df["Title"].str.strip() == serviceitems_text.strip()]
                        if not _match.empty:
                            _btn_url = str(_match.iloc[0].get("Button", "") or "").strip()
                            if not _btn_url.startswith("http"):
                                _btn_url = None
                except Exception:
                    pass

                # 3b. Known default URLs (sheet overrides these; kept so items
                #     work even before the sheet Button column is filled in)
                if not _btn_url:
                    _btn_url = _KNOWN_BUTTON_URLS.get(serviceitems_text)

                if _btn_url:
                    await send_whatsapp_interactive_link(sender_id, _btn_url)
                    _fup_key = _get_followup_key(serviceitems_text)
                    persistent_mode_sessions[sender_id] = _fup_key
                    await _send_followup_prompt(sender_id, serviceitems_text)
                    return

                # ── 4. Universal AI chat fallback ─────────────────────────────
                # Every unhandled item enters a persistent AI chat mode.
                # chat_type is inferred from the item title for best relevance.
                chat_type = _infer_chat_type(serviceitems_text)
                mode_key = re.sub(r'[^\w]', '', serviceitems_text.lower().replace(" ", "_"))
                persistent_mode_sessions[sender_id] = mode_key
                data = await _whatsapp_ai_chat(
                    sender_id,
                    f"I selected '{serviceitems_text}' from the menu. Please give me relevant information about this in Bali.",
                    chat_type,
                )
                if data:
                    await send_whatsapp_message(sender_id, data)
                    _fup_key = _get_followup_key(serviceitems_text)
                    persistent_mode_sessions[sender_id] = _fup_key
                    await _send_followup_prompt(sender_id, serviceitems_text)
                # None means booking flow sent or error — no extra message needed
                return

            else:
                if message_text:
                    # ── Intent-based routing for common free-text queries ────────────
                    # Routes natural-language queries to the right specialised AI
                    # instead of the generic WhatsApp chatbot.
                    _FREE_TEXT_INTENTS = [
                        (["what to do today", "what should i do today", "today activities", "things to do today"], "what_to_do_today"),
                        (["plan my trip", "plan a trip", "trip plan", "itinerary", "day plan"], "plan_my_trip"),
                        (["things to do in bali", "bali activities", "things to do", "what to do in bali"], "things_to_do_in_bali"),
                        (["event calendar", "events today", "festivals", "whats on", "what's on"], "event_calendar"),
                        (["local cuisine", "food guide", "where to eat", "restaurants", "bali food"], "local_cousine_guide"),
                    ]
                    _msg_lower = message_text.strip().lower()
                    _routed = False
                    for _phrases, _mode_key in _FREE_TEXT_INTENTS:
                        if any(_p in _msg_lower for _p in _phrases):
                            _chat_type = PERSISTENT_MODE_CHAT_TYPES.get(_mode_key)
                            if _chat_type:
                                persistent_mode_sessions[sender_id] = _mode_key
                                _data = await _whatsapp_ai_chat(sender_id, message_text, _chat_type)
                                if _data:
                                    await send_whatsapp_message(sender_id, _data)
                                _routed = True
                                break
                    if _routed:
                        return
                    # ────────────────────────────────────────────────────────────────

                    # ── D&P free-text intercept: show the REAL menu, not AI prose ──
                    # Root cause (Adam/Clay, 2026-08-15): a guest typing "discounts
                    # and promotions" got an AI text description of promos instead
                    # of the actual interactive Discounts & Promotions Flow card —
                    # inconsistent with how every other menu-equivalent free-text
                    # query on this platform works (see _FREE_TEXT_INTENTS above,
                    # and WCR-13's specific-service-list-before-Category-Flow
                    # pattern for services). Mirrors the discount__promotions tap
                    # handler's exact send + fallback pattern verbatim.
                    _DNP_FREE_TEXT_PHRASES = [
                        "discount", "discounts", "promotion", "promotions", "promo",
                        "promos", "deal", "deals", "voucher", "vouchers", "offer",
                        "offers", "special offer",
                    ]
                    if any(p in _msg_lower for p in _DNP_FREE_TEXT_PHRASES):
                        _dnp_ft_sent = False
                        try:
                            from app.services.whatsapp_flows_service import send_dnp_flow_message as _send_dnp_flow_ft
                            await _send_dnp_flow_ft(sender_id, f"dnp_{sender_id[:20]}")
                            _dnp_ft_sent = True
                        except Exception as _dnp_ft_err:
                            logger.warning(f"[D&P free-text] flow send failed (falling back to list): {_dnp_ft_err}")
                        if not _dnp_ft_sent:
                            await _send_dnp_categories(sender_id)
                        return
                    # ────────────────────────────────────────────────────────────────

                    query_for_ai = await _enrich_currency_query(message_text) if _is_currency_query(message_text) else message_text
                    ai_result = await whatsapp_response(query_for_ai, sender_id, user_villa_code or "WEB_VILLA_01")

                    print(ai_result)
                    
                    if ai_result:
                        # Extract intent and subcategory BEFORE sending text so the intro
                        # message matches the flow card (not the AI's uncertain phrasing).
                        _ai_intent = ai_result.get("intent") or {}
                        _wants_booking_menu = bool(ai_result.get("should_send_menu") or _ai_intent.get("category"))
                        _ai_subcategory = (_ai_intent.get("subcategory") or "").strip()
                        _ai_category = (_ai_intent.get("category") or "").strip()

                        # WCR-15: Generic order intent — "I want to order service",
                        # "want to order", "order service" etc. name no specific service
                        # so detect_service_intent returns nothing and should_send_menu stays
                        # False. Force the Category Flow so the guest can browse and book.
                        if not _wants_booking_menu and user_villa_code:
                            _GENERIC_ORDER_WA = {
                                "i want to order", "want to order", "order service",
                                "i would like to order", "place an order", "i'd like to order",
                            }
                            if any(p in (message_text or "").lower() for p in _GENERIC_ORDER_WA):
                                _wants_booking_menu = True

                        _will_send_sub_flow = bool(_ai_subcategory and _wants_booking_menu and user_villa_code)
                        if _will_send_sub_flow:
                            await send_whatsapp_message(sender_id,
                                f"✨ Here are the *{_ai_subcategory}* options available at your villa — tap to view and book:")
                        else:
                            await send_whatsapp_message(sender_id, ai_result["text"])
                        if _wants_booking_menu and user_villa_code:
                            if _ai_subcategory:
                                try:
                                    from app.services.whatsapp_flows_service import send_category_flow_message as _send_sub_flow
                                    _sub_flow_token = f"sub|{sender_id}|{_ai_subcategory}|{_ai_category}|{user_villa_code}"
                                    _sub_flow_id = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                                    await _send_sub_flow(
                                        sender_id,
                                        _sub_flow_id,
                                        _sub_flow_token,
                                        cta="View Options",
                                        header_text=_ai_subcategory[:60],
                                        body_text="Tap an option below to select and book.",
                                    )
                                except Exception as _wcr18_err:
                                    logger.error(f"[WCR-18] subcategory flow failed (non-fatal): {_wcr18_err}")
                            else:
                                # ── WCR-13: specific service list → fallback when no subcategory ──
                                # send_ai_whatsapp_list_message when AI did not detect a subcategory
                                _ai_menu_data = ai_result.get("menu_data")
                                if _ai_menu_data and _ai_menu_data.get("sections"):
                                    try:
                                        _wa_sects = []
                                        for _s in (_ai_menu_data.get("sections") or []):
                                            _wa_rows = []
                                            for _r in _s.get("rows", []):
                                                _pr = _r.get("price", "")
                                                _ds = _r.get("description", "")
                                                _wa_desc = (
                                                    f"💰 {_pr} — {_ds}" if _pr and _ds
                                                    else f"💰 {_pr}" if _pr
                                                    else _ds
                                                )[:72]
                                                _wa_rows.append({**_r, "description": _wa_desc})
                                            _wa_sects.append({**_s, "rows": _wa_rows})
                                        _wa_md = {**_ai_menu_data, "sections": _wa_sects}
                                        await send_ai_whatsapp_list_message(
                                            sender_id, _wa_md,
                                            button_text="View & Book",
                                            footer_text="Tap an option to proceed | GINI Bali ✨"
                                        )
                                    except Exception as _wcr13_err:
                                        logger.error(f"[WCR-13] service list failed (non-fatal): {_wcr13_err}")
                                else:
                                    # WCR-15: Generic Category Flow fallback — no subcategory,
                                    # no specific service list. Guest wants to order but didn't
                                    # name a service (e.g. "I want to order service"). Send the
                                    # full category browser so they can choose from all services.
                                    try:
                                        from app.services.whatsapp_flows_service import send_category_flow_message as _gen_cat
                                        import uuid as _ugc
                                        _gen_tok = f"cat_{sender_id}_{_ugc.uuid4().hex[:8]}"
                                        _gen_fid = settings.WHATSAPP_CATEGORY_FLOW_ID or "1465038141489393"
                                        await _gen_cat(
                                            sender_id, _gen_fid, _gen_tok,
                                            cta="Browse Services",
                                            body_text="Explore all services and book in a few taps:",
                                        )
                                    except Exception as _gen_cat_err:
                                        logger.error(f"[WCR-15 generic flow] non-fatal: {_gen_cat_err}")

                        else:
                            # Task 19: Log Inquiry for Tracking (no menu shown)
                            asyncio.create_task(log_guest_inquiry(
                                sender_id,
                                user_villa_code or "WEB_VILLA_01",
                                message_text,
                                ai_result["text"]
                            ))
                    else:
                        await send_whatsapp_message(
                            sender_id,
                            "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. "
                            "Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581."
                        )
    except Exception as e:
        print(f"Error processing message from {sender_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        try:
            await send_whatsapp_message(
                sender_id,
                "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. "
                "Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581."
            )
        except Exception:
            pass  # never let fallback crash the handler
    finally:
        end_time = datetime.datetime.now()
        latency = (end_time - start_time).total_seconds()
        logger.info(f"⏱️ Finished processing message {message_id}. Latency: {latency:.2f}s")
        # Log latency to DB for monitoring
        try:
            await db["analytics_latency"].insert_one({
                "message_id": message_id,
                "sender_id": sender_id,
                "latency_seconds": latency,
                "timestamp": datetime.datetime.utcnow()
            })
        except:
            pass



async def _send_text_message_raw(recipient_id: str, message: str) -> tuple:
    """
    Low-level freeform text sender.
    Returns (success: bool, meta_error_code: int | None).
    meta_error_code is the numeric code from Meta's error body (e.g. 131026, 131047).
    """
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_id,
            "text": {"body": message},
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            if response.status_code >= 400:
                meta_code = None
                try:
                    meta_code = response.json().get("error", {}).get("code")
                    if meta_code is not None:
                        meta_code = int(meta_code)
                except Exception:
                    pass
                logger.error(
                    f"❌ WhatsApp API Error (_send_text_message_raw): "
                    f"recipient={recipient_id} status={response.status_code} "
                    f"meta_code={meta_code} body={response.text[:300]}"
                )
                return False, meta_code
            response.raise_for_status()
            return True, None
    except Exception as e:
        logger.error(f"❌ WhatsApp send exception: recipient={recipient_id} error={e}")
        return False, None


async def send_whatsapp_message(recipient_id: str, message: str) -> bool:
    """
    Send a simple text message to WhatsApp.
    Returns True if the API accepted the message (2xx), False otherwise.
    """
    # NOTE (2026-08-07, WCR-22): a substring-matching outgoing-message safety guard
    # that used to live here (WCR-21) has been removed. It matched the opening
    # words of the area-picker question and, on match, deleted the guest's
    # in-progress villa-code session and silently substituted an unrelated
    # Category Flow card. Those words are also the exact opening of the CURRENT,
    # intentional villa-onboarding area-picker message (_vc_session
    # "awaiting_location_choice" branch, ~line 5580) — so the guard was firing
    # against the app's own live onboarding flow, not the deprecated format it
    # was written to catch. Root cause of the "villa code not asked" /
    # "main menu not working" reports (2026-08-06/07): confirmed via production
    # BLOCKED: MISSING_VILLA_CODE errors and a direct source-string collision.
    # Nothing else in the codebase generates the deprecated pattern anymore (grep
    # confirmed), so the guard had no remaining protective purpose. Do not
    # reintroduce a substring-based guard on live onboarding message text — if a
    # new safety net is ever needed, gate it on something that cannot collide with
    # current, intentional message content.
    ok, _ = await _send_text_message_raw(recipient_id, message)
    return ok


async def send_whatsapp_template(
    recipient: str,
    template_name: str,
    template_vars: list[str],
    language_code: str = "en",
) -> bool:
    """
    Send an approved WhatsApp template message — bypasses 24-hour window.
    Returns True if Meta accepted the message.
    """
    try:
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": str(v)} for v in template_vars
                        ],
                    }
                ],
            },
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
            if resp.status_code >= 400:
                logger.error(
                    f"[Template] {template_name} to {recipient}: HTTP {resp.status_code} — {resp.text[:500]}"
                )
                return False
            logger.info(f"[Template] {template_name} sent to {recipient}")
            return True
    except Exception as e:
        logger.error(f"[Template] {template_name} exception for {recipient}: {e}")
        return False


async def send_with_fallback(
    recipient: str,
    template_name: str,
    template_vars: list[str],
    freeform_msg: str,
    label: str = "",
) -> bool:
    """
    Try template first (bypasses 24-hr window), fall back to freeform text.
    Returns True if either channel succeeded.
    """
    ok = await send_whatsapp_template(recipient, template_name, template_vars)
    if ok:
        return True
    logger.warning(f"[{label}] Template failed for {recipient}, trying freeform fallback")
    ok = await send_whatsapp_message(recipient, freeform_msg)
    if ok:
        return True
    logger.error(f"[{label}] BOTH template and freeform failed for {recipient}")
    return False


# Meta error codes that indicate the 24-hour customer service window has closed.
_WA_WINDOW_ERROR_CODES = frozenset({131026, 131047})


async def send_whatsapp_with_fallback(
    recipient: str,
    freeform_msg: str,
    template_name: str,
    template_vars: list,
    notification_type: str,
    order_number: str = "N/A",
) -> bool:
    """
    Freeform-first notification with template fallback for 24-hour window errors.

    Attempt 1: send freeform text (richer content, works within 24-hr window).
    Attempt 2: if Meta returns 131026 or 131047 (window expired), retry with
               an approved template that bypasses the restriction.

    Every attempt — success or failure — is written to notification_log so
    no delivery event is ever silent.

    Returns True if any channel succeeded.
    """
    from app.utils.notification_logger import log_notification_attempt

    ok, meta_code = await _send_text_message_raw(recipient, freeform_msg)
    await log_notification_attempt(
        recipient=recipient,
        notification_type=notification_type,
        channel="freeform",
        success=ok,
        order_number=order_number,
        meta_error_code=meta_code,
    )
    if ok:
        return True

    if meta_code not in _WA_WINDOW_ERROR_CODES:
        logger.error(
            f"[{notification_type}] freeform failed for {recipient} "
            f"with meta_code={meta_code} (not a window error) — no template fallback"
        )
        return False

    logger.info(
        f"[{notification_type}] 24-hr window error {meta_code} for {recipient} "
        f"— retrying with template '{template_name}'"
    )
    ok = await send_whatsapp_template(recipient, template_name, template_vars)
    await log_notification_attempt(
        recipient=recipient,
        notification_type=notification_type,
        channel="template",
        success=ok,
        order_number=order_number,
        template_name=template_name,
        error_detail="24hr_window_fallback",
    )
    if not ok:
        logger.error(
            f"[{notification_type}] BOTH freeform and template failed for {recipient} "
            f"order={order_number}"
        )
    return ok


async def send_sp_payment_confirmation(order_data: dict) -> dict:
    """
    Notify SP that payment has been confirmed — uses approved template
    with freeform fallback. Returns {sent, number, reason}.
    """
    order_number = order_data.get("order_number", "N/A")
    service_name = order_data.get("service_name", "N/A")
    villa_code = order_data.get("villa_code", "")

    appointment_date = order_data.get("booking_date") or order_data.get("date")
    if appointment_date and hasattr(appointment_date, "strftime"):
        appointment_date = appointment_date.strftime("%d %B %Y")
    elif not isinstance(appointment_date, str) or not appointment_date:
        appointment_date = "N/A"

    appointment_time = order_data.get("time") or "As scheduled"

    villa_name = "N/A"
    villa_location = order_data.get("location_zone") or "N/A"
    if villa_location.startswith("V") and len(villa_location) <= 4:
        villa_location = "N/A"
    try:
        _vinfo = await get_villa_info_by_code(villa_code) if villa_code else None
        if _vinfo:
            villa_name = _vinfo.get("name") or "N/A"
            villa_location = _vinfo.get("location") or villa_location
    except Exception:
        pass

    _raw_sender = order_data.get("sender_id", "")
    guest_contact = (
        order_data.get("phone_number")
        or (_raw_sender if str(_raw_sender).isdigit() else None)
        or "Not provided"
    )

    freeform_msg = (
        f"🎉 *Payment Confirmed!* 🎉\n\n"
        f"The customer has paid for the service. Kindly provide the service as scheduled.\n\n"
        f"📌 *Order Details:*\n"
        f"• *Order #:* {order_number}\n"
        f"• *Service:* {service_name}\n"
        f"• *Date:* {appointment_date}\n"
        f"• *Time:* {appointment_time}\n"
        f"• *Villa:* {villa_name}\n"
        f"• *Location:* {villa_location}\n"
        f"• *Customer Contact:* {guest_contact}\n"
        f"• *Status:* PAID ✅\n\n"
        f"Please coordinate directly with the guest to ensure a smooth delivery. Thank you!"
    )

    # Template vars: {{1}}=order, {{2}}=service, {{3}}=date, {{4}}=time,
    #                {{5}}=villa, {{6}}=location, {{7}}=customer_contact
    template_vars = [
        order_number, service_name, str(appointment_date),
        str(appointment_time), villa_name, villa_location, str(guest_contact),
    ]

    # Collect SP numbers to notify.
    # SPN-ONLY-ACCEPTING-SP (Adam/Clay bug, 2026-08-13): the post-payment
    # "Payment Confirmed" message must go ONLY to the SP that accepted the
    # request (confirmed_by_provider) — never to every SP assigned to the service.
    # By payment time an SP has ALWAYS accepted: the payment link is issued only
    # AFTER acceptance on BOTH channels (chatbot_routes.create_booking_payment
    # returns payment_url=None until accept; the WhatsApp accept handler sets
    # confirmed_by_provider atomically). The broad fetch_whatsapp_numbers() lookup
    # is therefore retained ONLY as a fallback for the edge case where no accepting
    # SP is on record (legacy/manual orders) — it must NEVER run in addition to a
    # known accepting SP, which is what caused the reported "all SPs notified" bug.
    sp_numbers = []
    confirmed_sp = order_data.get("confirmed_by_provider")
    if confirmed_sp and str(confirmed_sp).isdigit():
        # Normal path: exactly the SP that accepted this booking.
        sp_numbers.append(str(confirmed_sp))
    else:
        # Fallback ONLY when no accepting SP is recorded — never alongside one.
        logger.warning(
            f"[SP-Payment] No confirmed_by_provider for order {order_number}; "
            f"falling back to service-level SP lookup."
        )
        try:
            lookup_numbers = await fetch_whatsapp_numbers(service_name, villa_location)
            for num in lookup_numbers:
                if num and num not in sp_numbers:
                    sp_numbers.append(num)
        except Exception as _e:
            logger.warning(f"[SP-Payment] Lookup fallback failed for {order_number}: {_e}")

    if not sp_numbers:
        logger.error(f"[SP-Payment] No SP numbers found for order {order_number}")
        return {"sent": False, "number": "none", "reason": "no_sp_numbers_found"}

    from app.utils.notification_logger import log_notification_attempt

    sent_any = False
    sent_numbers = []
    for num in sp_numbers:
        ok = await send_with_fallback(
            num, "sp_payment_confirmation", template_vars, freeform_msg,
            label=f"SP-Payment-{order_number}",
        )
        await log_notification_attempt(
            recipient=num,
            notification_type="payment_confirmed_sp",
            channel="template_first",
            success=ok,
            order_number=order_number,
            template_name="sp_payment_confirmation",
        )
        if ok:
            sent_any = True
            sent_numbers.append(num)

    logger.info(
        f"[SP-Payment] order={order_number} sent_to={sent_numbers} "
        f"attempted={sp_numbers} success={sent_any}"
    )
    return {
        "sent": sent_any,
        "number": ",".join(sent_numbers) if sent_numbers else ",".join(sp_numbers),
        "reason": None if sent_any else "all_channels_failed",
    }


async def send_villa_commission_notification(
    order_data: dict,
    disbursement_results: dict,
) -> bool:
    """
    Notify the villa manager about their commission — uses approved template
    with freeform fallback. Returns True if notification was delivered.

    DISABLED 2026-07-06 (Clay/user decision): villa commission payouts are
    handled manually, so this automated message was noise and confusing.
    The early return below suppresses the send. It returns True so the
    caller's retry/alert logic does not fire. The body is retained
    (unreachable) so the notification contract stays intact for guardrails
    and so the feature can be re-enabled by removing this single return.
    """
    return True

    order_number = order_data.get("order_number", "N/A")
    villa_code = order_data.get("villa_code", "")
    if not villa_code:
        logger.warning(f"[Villa-Commission] No villa_code on order {order_number}")
        return False

    villa_phone = await get_villa_whatsapp_by_code(villa_code)
    if not villa_phone:
        logger.error(f"[Villa-Commission] No WhatsApp number for villa {villa_code}")
        return False

    _vinfo = None
    try:
        _vinfo = await get_villa_info_by_code(villa_code)
    except Exception:
        pass
    villa_name = (_vinfo.get("name") if _vinfo else None) or "N/A"

    dist_data = order_data.get("payment", {}).get("distribution_data", {})
    villa_amount = dist_data.get("villa", {}).get("amount", 0)
    service_name = order_data.get("service_name", "N/A")

    vl_res = disbursement_results.get("villa", {})
    if vl_res.get("success"):
        villa_status = vl_res.get("status", "PENDING")
        status_text = (
            "disbursed to your registered bank account. ✅"
            if villa_status == "COMPLETED"
            else "initiated and is currently PENDING. ⏳"
        )
    else:
        skip_detail = f" ({vl_res.get('reason') or vl_res.get('error') or 'unknown'})"
        status_text = f"NOT automatically transferred{skip_detail}. GINI Bali will process this manually."

    freeform_msg = (
        f"🏡 *Commission Notice*\n\n"
        f"*Villa:* {villa_name}\n"
        f"*Order #:* {order_number}\n"
        f"*Service:* {service_name}\n"
        f"*Commission:* IDR {int(villa_amount):,}\n\n"
        f"The payment has been {status_text}"
    )

    # Template vars: {{1}}=villa, {{2}}=order, {{3}}=service,
    #                {{4}}=amount, {{5}}=status
    template_vars = [
        villa_name, order_number, service_name,
        f"IDR {int(villa_amount):,}", status_text,
    ]

    from app.utils.notification_logger import log_notification_attempt

    ok = await send_with_fallback(
        villa_phone, "villa_commission_notification", template_vars, freeform_msg,
        label=f"Villa-Commission-{order_number}",
    )
    await log_notification_attempt(
        recipient=villa_phone,
        notification_type="villa_commission",
        channel="template_first",
        success=ok,
        order_number=order_number,
        template_name="villa_commission_notification",
    )

    if not ok:
        logger.error(
            f"[Villa-Commission] FAILED for order {order_number}, "
            f"villa={villa_code}, phone={villa_phone}"
        )
    return ok


async def detect_stuck_villa_retries(stale_threshold_minutes: int = 20) -> None:
    """
    Startup detector for villa retry loops that were interrupted (e.g. Render restart).

    If an order has villa_notify_retry_active=True but last_attempt_at is older than
    stale_threshold_minutes, the retry loop is dead. This function:
      - Resets villa_notify_retry_active to False
      - Persists a stuck flag + timestamp
      - Alerts admin for each affected order

    Safe to call at every startup — idempotent, read-only on financial fields.
    """
    import datetime
    import os
    from app.db.session import order_collection

    try:
        cutoff = datetime.datetime.now() - datetime.timedelta(minutes=stale_threshold_minutes)
        cutoff_iso = cutoff.isoformat()

        # Find orders with an active retry flag where the last attempt is stale
        cursor = order_collection.find({
            "villa_notify_retry_active": True,
            "villa_notify_last_attempt_at": {"$lt": cutoff_iso},
        })
        stuck_orders = await cursor.to_list(length=50)

        if not stuck_orders:
            logger.info("[Villa-Commission-StuckDetector] No stuck retry loops found at startup")
            return

        logger.warning(
            f"[Villa-Commission-StuckDetector] Found {len(stuck_orders)} stuck retry loop(s) — resetting"
        )

        admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")

        for order in stuck_orders:
            order_number = order.get("order_number", "unknown")
            villa_code = order.get("villa_code", "unknown")
            attempt_count = order.get("villa_notify_attempt_count", 0)
            last_attempt = order.get("villa_notify_last_attempt_at", "unknown")
            last_error = order.get("villa_notify_last_error", "unknown")

            logger.error(
                f"[Villa-Commission-StuckDetector] Stuck order {order_number}: "
                f"villa={villa_code}, attempts={attempt_count}, last_attempt={last_attempt}"
            )

            # Reset the active flag — notification/audit fields only
            try:
                await order_collection.update_one(
                    {"order_number": order_number},
                    {"$set": {
                        "villa_notify_retry_active":              False,
                        "villa_notify_stuck_detected_at":         datetime.datetime.now().isoformat(),
                        "villa_notify_stuck_attempt_count":       attempt_count,
                        "payment.admin_summary.villa_notified":   False,
                        "payment.admin_summary.villa_notification_exhausted": True,
                        "payment.admin_summary.villa_notification_exhausted_at": datetime.datetime.now().isoformat(),
                    }},
                )
            except Exception as db_err:
                logger.error(
                    f"[Villa-Commission-StuckDetector] DB reset failed for {order_number}: {db_err}"
                )

            # Alert admin for each stuck order
            try:
                await send_whatsapp_message(
                    admin_number,
                    f"⚠️ *Villa Notification — Stuck Retry Detected*\n\n"
                    f"Order *{order_number}* (villa {villa_code}) had an active retry loop "
                    f"that was interrupted (process restart).\n\n"
                    f"*Attempts completed before restart:* {attempt_count}\n"
                    f"*Last attempt:* {last_attempt}\n"
                    f"*Last error:* {last_error}\n\n"
                    f"The villa manager has NOT been notified. Please notify manually."
                )
            except Exception as alert_err:
                logger.error(
                    f"[Villa-Commission-StuckDetector] Admin alert failed for {order_number}: {alert_err}"
                )

    except Exception as e:
        logger.error(f"[Villa-Commission-StuckDetector] Detector failed: {e}")


async def retry_villa_commission_notification(
    order_number: str,
    order_data: dict,
    disbursement_results: dict,
) -> None:
    """
    Background retry task for villa commission notification.

    Schedule:
      Round 1 — 3 attempts, 60s apart
      If all fail → wait 5 minutes
      Round 2 — 3 attempts, 60s apart
      If all fail → persist exhausted flag + alert admin

    Safety guarantees:
      - Re-resolves villa contact on every attempt (no stale phone number)
      - Persists attempt count + last error to DB on every attempt
      - Only modifies notification/audit fields — never touches financial state
      - Single-flight enforced in xendit_webhook.py before task is created

    Started as asyncio.create_task() from xendit_webhook.py.
    Never raises — all failures are handled internally.
    """
    import asyncio
    import datetime
    import os
    from app.db.session import order_collection

    ATTEMPT_GAP = 60       # seconds between attempts within a round
    ROUND_PAUSE = 300      # seconds between round 1 and round 2
    ROUNDS = [3, 3]        # attempts per round

    villa_code = order_data.get("villa_code", "unknown")
    attempt_number = 0
    last_error = "unknown"

    for round_idx, max_attempts in enumerate(ROUNDS):
        if round_idx > 0:
            logger.info(
                f"[Villa-Commission-Retry] Order {order_number}: "
                f"pausing {ROUND_PAUSE}s before round {round_idx + 1}"
            )
            await asyncio.sleep(ROUND_PAUSE)

        for i in range(max_attempts):
            attempt_number += 1
            if i > 0:
                await asyncio.sleep(ATTEMPT_GAP)

            logger.info(
                f"[Villa-Commission-Retry] Order {order_number}: "
                f"attempt {attempt_number} (round {round_idx + 1}, {i + 1}/{max_attempts})"
            )

            # Re-resolve contact on every attempt — do not reuse stale number
            ok = False
            try:
                # Re-fetch order so any manual fix to villa_code is picked up
                fresh_order = await order_collection.find_one({"order_number": order_number})
                target_order = fresh_order if fresh_order else order_data
                ok = await send_villa_commission_notification(target_order, disbursement_results)
                if not ok:
                    last_error = "send_with_fallback returned False (template + freeform both failed)"
            except Exception as e:
                last_error = str(e)
                logger.error(
                    f"[Villa-Commission-Retry] Order {order_number}: "
                    f"attempt {attempt_number} raised exception: {e}"
                )

            # Persist attempt state — notification/audit fields only
            try:
                await order_collection.update_one(
                    {"order_number": order_number},
                    {"$set": {
                        "villa_notify_attempt_count":    attempt_number,
                        "villa_notify_last_attempt_at":  datetime.datetime.now().isoformat(),
                        "villa_notify_last_error":        None if ok else last_error,
                    }},
                )
            except Exception as db_err:
                logger.error(
                    f"[Villa-Commission-Retry] DB state update failed for {order_number} "
                    f"attempt {attempt_number}: {db_err}"
                )

            if ok:
                logger.info(
                    f"[Villa-Commission-Retry] Order {order_number}: "
                    f"notification delivered on attempt {attempt_number}"
                )
                try:
                    await order_collection.update_one(
                        {"order_number": order_number},
                        {"$set": {
                            "villa_notify_retry_active":              False,
                            "payment.admin_summary.villa_notified":   True,
                            "villa_notify_success_attempt":           attempt_number,
                        }},
                    )
                except Exception as db_err:
                    logger.error(
                        f"[Villa-Commission-Retry] DB success update failed for {order_number}: {db_err}"
                    )
                return  # success — stop all retries

    # All 6 attempts exhausted — persist flag and alert admin
    logger.error(
        f"[Villa-Commission-Retry] EXHAUSTED all {attempt_number} attempts for order {order_number}. "
        f"Villa manager was NOT notified. Last error: {last_error}"
    )

    try:
        await order_collection.update_one(
            {"order_number": order_number},
            {"$set": {
                "villa_notify_retry_active":                          False,
                "payment.admin_summary.villa_notified":               False,
                "payment.admin_summary.villa_notification_exhausted": True,
                "payment.admin_summary.villa_notification_exhausted_at": datetime.datetime.now().isoformat(),
            }},
        )
    except Exception as db_err:
        logger.error(
            f"[Villa-Commission-Retry] Failed to persist exhausted flag for {order_number}: {db_err}"
        )

    try:
        admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
        await send_whatsapp_message(
            admin_number,
            f"⚠️ *Villa Notification Failed — Action Required*\n\n"
            f"Order *{order_number}* (villa {villa_code}) completed payment and disbursement, "
            f"but the villa manager could NOT be notified after {attempt_number} attempts over ~13 minutes.\n\n"
            f"*Last error:* {last_error}\n\n"
            f"*Possible causes:*\n"
            f"• Villa phone number missing or wrong in Villas sheet\n"
            f"• Meta API unavailable\n\n"
            f"Please notify the villa manager manually."
        )
    except Exception as alert_err:
        logger.error(
            f"[Villa-Commission-Retry] Admin alert also failed for {order_number}: {alert_err}"
        )


# async def notify_payment_completion(order_data: dict):
#     try:
#         sender_id = order_data.get("sender_id")
#         order_number = order_data.get("order_number")
#         service_name = order_data.get("service_name")
#         if sender_id:
#             completion_message = (
#                 f"✅ Payment Confirmed!\n\n"
#                 f"Order {order_number} for {service_name} has been paid successfully. "
#                 f"The service provider has been notified and will contact you shortly."
#             )
#             await send_whatsapp_message(sender_id, completion_message)
#         service_provider_code = order_data.get("confirmed_by_provider")
#         if service_provider_code:
#             provider_message = (
#                 f"💰 Payment Received!\n\n"
#                 f"Order {order_number} has been paid. Please proceed with the service delivery."
#             )
#             await send_whatsapp_message(service_provider_code, provider_message)
            
#     except Exception as e:
#         print(f"Notification error: {str(e)}")


async def notify_payment_completion(order_data: dict) -> dict:
    """
    Notify guest, SP, and admin of a completed payment.
    Returns a dict with actual send outcomes — do NOT infer success from
    the fact this function returned; always check the returned flags.
    """
    import datetime as _dt
    from app.db.session import order_collection as _oc

    sp_result = {"sent": False, "number": "unknown", "reason": "not_attempted"}
    guest_ok = False
    guest_error = None
    try:
        sender_id = order_data.get("sender_id")
        order_number = order_data.get("order_number")
        service_name = order_data.get("service_name")

        if not sender_id:
            logger.warning(f"No sender_id found for order {order_number}")
            return {"sp_notified": False, "sp_number": "unknown", "sp_reason": "no_sender_id"}

        # Determine connection type
        is_whatsapp = sender_id.isdigit()
        is_websocket = not is_whatsapp

        # 1. Notify Customer
        completion_message = (
            f"🌟 ***Booking Confirmed & Paid!*** 🌟\n\n"
            f"Hi! We've successfully received your payment for your **{service_name}** (Order: {order_number}).\n\n"
            f"Our service provider has been notified and will coordinate final details with you shortly. "
            f"Thank you for choosing GINI Bali! 🌴\n\n"
            f"💬 Want to book another service? Just reply *menu* anytime to get started."
        )
        try:
            if is_whatsapp:
                guest_ok = await send_whatsapp_with_fallback(
                    recipient=sender_id,
                    freeform_msg=completion_message,
                    template_name="payment_confirmed_guest",
                    template_vars=[service_name or "N/A", order_number or "N/A"],
                    notification_type="payment_confirmed_guest",
                    order_number=order_number or "N/A",
                )
            elif is_websocket:
                # Using message_type="text" ensures maximum compatibility for simple rendering
                await manager.send_personal_message(
                    message=completion_message,
                    session_id=sender_id,
                    message_type="text"
                )
                guest_ok = True
        except Exception as _ge:
            guest_error = str(_ge)
            logger.error(f"Notification error in notify_payment_completion: {guest_error}")

        # 2. Notify Service Provider — capture actual outcome
        sp_result = await notify_service_provider(order_data)

        # 3. Villa commission notification is handled in xendit_webhook.py with
        #    the actual commission amount from distribution_data. Sending it here
        #    would duplicate the message. Removed to avoid double-notification.

        # 4. Notify Easy-Bali Admin
        await notify_admin_of_outcome(order_data, "SUCCESS")

    except Exception as e:
        logger.error(f"Notification error in notify_payment_completion: {str(e)}")
        guest_ok = False
        guest_error = str(e)

    # 5. Persist notification outcomes to DB (visibility only — no retry)
    _now = _dt.datetime.now().isoformat()
    try:
        await _oc.update_one(
            {"order_number": order_data.get("order_number")},
            {"$set": {
                "payment.admin_summary.guest_payment_notified":    guest_ok,
                "payment.admin_summary.guest_payment_notified_at": _now if guest_ok else None,
                "payment.admin_summary.guest_payment_notify_error": guest_error,
                "payment.admin_summary.sp_payment_notified":       sp_result.get("sent", False),
                "payment.admin_summary.sp_payment_notified_at":    _now if sp_result.get("sent") else None,
                "payment.admin_summary.sp_payment_notify_error":   sp_result.get("reason") if not sp_result.get("sent") else None,
            }},
        )
    except Exception as _dbe:
        logger.error(f"Notification tracking DB write failed for {order_data.get('order_number')}: {_dbe}")

    return {
        "guest_notified": guest_ok,
        "sp_notified": sp_result.get("sent", False),
        "sp_number":   sp_result.get("number", "unknown"),
        "sp_reason":   sp_result.get("reason"),
    }

async def notify_payment_failure(order_data: dict, reason: str):
    """Notify relevant parties about payment failure/expiry."""
    try:
        await notify_admin_of_outcome(order_data, f"FAILURE: {reason}")
    except Exception as e:
        logger.error(f"Error in notify_payment_failure: {e}")

async def notify_admin_of_outcome(order_data: dict, outcome: str):
    """Unified admin notification for booking outcomes."""
    try:
        order_number = order_data.get("order_number")
        service_name = order_data.get("service_name")
        price = order_data.get("price")
        villa_code = order_data.get("villa_code")
        admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")

        if outcome == "SUCCESS":
            emoji = "💰"
            title = "REVENUE ALERT!"
            status_text = "Payment confirmed"
        else:
            emoji = "⚠️"
            title = "BOOKING ALERT (ACTION MAY BE NEEDED)"
            status_text = f"Payment {outcome}"

        admin_msg = (
            f"{emoji} ***{title}*** {emoji}\n\n"
            f"**Order:** {order_number}\n"
            f"**Status:** {status_text}\n"
            f"**Service:** {service_name}\n"
            f"**Villa:** {villa_code}\n"
            f"**Amount:** IDR {int(float(price)):,}\n\n"
            f"Check Dashboard: {settings.BASE_URL}/admin/dashboard"
        )
        await send_whatsapp_message(admin_number, admin_msg)
        
        # Placeholder for real email if needed in future
        logger.info(f"📧 Admin Notification (Email Simulated) for Order {order_number}: {status_text}")
        
    except Exception as e:
        logger.error(f"Admin notification error: {e}")

async def send_invoice_and_handle_closure(order_data: dict, invoice_result: dict, is_whatsapp: bool, is_websocket: bool):
    try:
        sender_id = order_data['sender_id']
        order_number = order_data['order_number']
        download_url = invoice_result['download_url']
        
        if is_whatsapp:
            await send_invoice_with_download(sender_id, download_url, order_number)
            
        elif is_websocket:
            invoice_message = (
                f"📄 ***Official Receipt Released***\n\n"
                f"Your receipt for **{order_data.get('service_name', 'your booking')}** (Order: {order_number}) is ready.\n\n"
                f"**Download Link:**\n"
                f"[Download Receipt]({download_url})\n\n"
                f"Thank you for choosing GINI Bali! 🌴"
            )
            
            # Send invoice message as text to ensure it's rendered immediately
            await manager.send_personal_message(
                message=invoice_message,
                session_id=sender_id,
                message_type="text"
            )
            # ⚠️ Removed "destroy" logic as it triggers clearWebSocketMessages in the frontend,
            # which wipes the chat history and makes these confirmation messages disappear.
            
            logger.info(f"✅ WebSocket invoice sent for session: {sender_id}")
            
            logger.info(f"✅ WebSocket connection closed for session: {sender_id}")
            
    except Exception as e:
        logger.exception(f"Invoice sending error: {str(e)}")
        if is_websocket:
            # Ensure we don't send destroy even on error
            logger.info(f"WebSocket cleanup handled gracefully for {sender_id}")

async def notify_service_provider(order_data: dict):
    """Delegate to the template-based SP payment confirmation."""
    return await send_sp_payment_confirmation(order_data)


async def send_booking_accepted_to_guest(
    recipient: str,
    order_number: str,
    service_name: str,
    payment_url: str,
) -> bool:
    """
    Notify the guest that their booking has been accepted and the payment link is ready.

    Attempt 1: CTA URL interactive message (tappable "Pay Now" button — best UX).
    Attempt 2: booking_accepted_guest template (plain text URL — bypasses 24-hr window).

    The CTA URL format is rejected by Meta outside the 24-hr window with no
    specific error code; any failure from send_interactive_message triggers the
    template fallback so the guest always receives the payment link.
    """
    from app.utils.notification_logger import log_notification_attempt

    # Attempt 1: CTA URL interactive message (works inside 24-hr window)
    cta_ok = False
    try:
        payment_result = {"payment_url": payment_url}
        result = await send_interactive_message(recipient, payment_result)
        cta_ok = result is not None
    except Exception as _e:
        logger.warning(f"[BookingAccepted] CTA send failed for {recipient} order {order_number}: {_e}")

    await log_notification_attempt(
        recipient=recipient,
        notification_type="booking_accepted_guest",
        channel="cta_interactive",
        success=cta_ok,
        order_number=order_number,
    )

    if cta_ok:
        return True

    # Attempt 2: approved template — bypasses 24-hr window
    logger.info(
        f"[BookingAccepted] CTA failed for {recipient}, retrying with template "
        f"'booking_accepted_guest' for order {order_number}"
    )
    template_ok = await send_whatsapp_template(
        recipient,
        "booking_accepted_guest",
        [service_name, order_number, payment_url],
    )
    await log_notification_attempt(
        recipient=recipient,
        notification_type="booking_accepted_guest",
        channel="template",
        success=template_ok,
        order_number=order_number,
        template_name="booking_accepted_guest",
        error_detail="cta_fallback",
    )
    if not template_ok:
        logger.error(
            f"[BookingAccepted] BOTH CTA and template failed for {recipient} order {order_number}"
        )
    return template_ok

async def get_villa_whatsapp_by_code(villa_code: str) -> str:
    """Returns the villa manager's WhatsApp number (digits only) for a given V-code.

    VM-NUMBER-SOURCE-PRIORITY-01 (2026-09-03, live report — Clay): "I put
    other number as VM (my 2nd number) and I didnt receive the
    notifications... the only reason the notification works before is
    because the system knows my number." Root cause: this function only
    ever read `manager_number` (the Villas Google Sheet's "Contact of VM"
    column, via the 15-min cache) -- it never checked the dashboard's
    editable Villa Profile `manager_phone` field (MongoDB `villa_profiles`,
    edited via POST /villa/profile). Three transactional notification paths
    (passport submission, maintenance issue, amenity request) all call this
    single function, so all three silently ignored a VM number entered via
    the dashboard. This is the exact same villa_profiles-takes-priority
    pattern already established elsewhere in this file (e.g. the WhatsApp
    issue-report villa-manager-notify block) -- this function was simply
    never updated to match it, even though those other call sites already
    fetch villa_profiles for other purposes.
    """
    try:
        from app.services.menu_services import get_villa_info_by_code
        vinfo = await get_villa_info_by_code(villa_code)
        rich_profile = None
        try:
            rich_profile = await db["villa_profiles"].find_one({"villa_code": villa_code})
        except Exception:
            pass  # non-fatal — fall through to the sheet-only value below
        raw = (rich_profile or {}).get("manager_phone") or (vinfo or {}).get("manager_number") or ""
        # Strip everything except digits — manager_number is a plain phone number, not a URL
        clean = re.sub(r'[^\d]', '', str(raw))
        return clean if clean else None
    except Exception as e:
        logger.error(f"Error fetching villa whatsapp for {villa_code}: {e}")
        return None
