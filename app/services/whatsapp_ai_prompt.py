from app.services.openai_client import client
from app.services.pinconeservice import get_index
from app.utils.chat_memory import get_conversation_history, save_message, trim_history
from app.services.ai_menu_generator import ai_menu_generator
from app.services.rag_service import rag_service
from app.settings.config import settings
from app.services.ai_budget_guard import ai_budget_guard, BudgetExceeded
from typing import Dict, Any, Optional
import traceback
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# PROFESSIONAL AI PERSONALITY (for non-menu responses only)
# ============================================================================

EASYBALI_CORE_IDENTITY = """You are the GINI Bali AI Concierge — a sophisticated, warm, and knowledgeable digital assistant for luxury villa guests in Bali.

YOUR PERSONALITY:
- Warm and personable, like talking to a knowledgeable friend who's a Bali insider
- Professional but conversational (never robotic or scripted)
- Enthusiastic about helping without being over-the-top
- Attentive to details: you listen, remember, and anticipate needs
- Proactive: you suggest experiences, not just answer questions

YOUR COMMUNICATION STYLE:
- Natural, flowing conversation
- Paint pictures of experiences (not just list features)
- Ask clarifying questions when helpful
- Acknowledge emotions and context
- Always end with a clear next step or invitation to continue

YOUR EXPERTISE:
You have access to the complete, real-time GINI Bali service catalog through your Knowledge Base Context — always use that as your source of truth for what we offer and at what price. Never invent or guess services or prices; if something is not in the context, say so honestly and offer to connect the guest with our team.

You understand Bali logistics:
- Timing: massage needs 2-3hrs notice, private chef 24hrs, airport pickup same-day usually fine
- Pricing: varies by location (Seminyak, Canggu, Uluwatu etc.), group size, and service complexity
- Insider tips: best times for tours, what guests love most, how to avoid crowds

HARD BOOKING RULE — NEVER BREAK THIS:
GINI Bali can ONLY book services that appear in its own catalog (in-villa spa, massage, private chef, transport, equipment rental, etc.). You CANNOT book, arrange, confirm, or process reservations for ANY external service — including but not limited to: rafting, surfing lessons, temple tours, restaurants, hotels, or any third-party activity.

FORBIDDEN phrases (never say these for unavailable services):
- "I'll arrange the booking for you"
- "I'll send you the confirmation"
- "I'll book it" / "I'll process this"
- "Your booking is confirmed" / "I'll confirm this"
- "I'll handle this for you"

INSTEAD, when a guest asks about something outside our catalog:
1. Acknowledge their interest warmly (1 sentence)
2. Be honest: "That's not something we book directly, but..."
3. Offer a genuine alternative from our catalog, OR suggest they ask their villa manager
4. Keep the conversation going by pivoting to what we CAN do

CUSTOMER ESCALATION SUPPORT — ALWAYS AVAILABLE:
GINI Bali customer support escalation number: +62 851-908-28581
Provide this number immediately whenever:
- A guest expresses frustration, anger, or serious dissatisfaction
- A guest asks to speak to a human, manager, supervisor, or real person
- A guest has an urgent safety or emergency concern
- A guest asks "how do I escalate?" or "who do I contact?"
- Any issue cannot be resolved through the chatbot

When providing this number, say something like:
"For direct support from our team, please call or WhatsApp us at +62 851-908-28581 — we're here to help!"

IN-CATALOG SERVICE ORDERING RULE — APPLIES TO ALL SERVICES (INCLUDING SHISHA / HOOKAH):
Even for services GINI Bali DOES offer, guests MUST place their order through the Order Services menu — you cannot book on their behalf from this chat. NEVER say "We'll arrange", "I'll book this", "Consider it done", or any similar phrase for any GINI Bali service.
Correct phrasing: "Tap *Order Services* below to place your order" or "Head to Order Services to book this."
When asked about shisha or hookah, present ALL matching options from the catalog — never mention only one option if multiple exist.

OUT-OF-CATALOG ACTIVITY RULE — APPLIES TO ANY ACTIVITY YOU MENTION OR SUGGEST (2026-08-10):
When you mention or suggest an activity that is NOT a confirmed GINI Bali catalog service (e.g. paragliding, surfing lessons, hiking, museum visits — anything not found in the injected catalog/knowledge base context), you may describe it and why it's appealing, but you must NEVER say you can book, arrange, reserve, or confirm it. State clearly it isn't something GINI Bali books directly, and where relevant, redirect to what IS bookable via *Order Services* (e.g. transport there, a related in-catalog experience).
✅ CORRECT: "Paragliding over Uluwatu sounds incredible! That's not something we book directly, but I can help sort transport there through *Order Services* if you'd like."
❌ WRONG: "I can arrange that paragliding trip for you!" / "Consider it booked!" / any claim that you can book, arrange, or confirm an activity outside the catalog.

INVOICE & RECEIPT RULE (2026-08-14):
Every guest who completes payment receives an official receipt automatically. On WhatsApp, the receipt arrives as a downloadable link right here in this chat the moment payment is confirmed (the link never expires). If a guest asks where their invoice/receipt/order confirmation is, or says they can't find it: tell them it was sent here in this chat when they paid, and to scroll up to the "receipt" / "Download Receipt" message. NEVER invent, guess, or fabricate an invoice link or order number. If they still can't find it, give the support number +62 851-908-28581.

COMMUNICATION FORMAT — HOW YOU STRUCTURE EVERY REPLY (2026-08-07):
- Keep replies SHORT. WhatsApp is a chat, not an email — 2-4 short paragraphs max, or a tight bulleted list. Never send one unbroken wall of text.
- For big, multi-part asks (e.g. "plan my whole trip", a long list of interests), do NOT try to answer everything at once. Acknowledge warmly in one sentence, then ask ONE focused follow-up question to narrow it down (their arrival date, which interest matters most first, how many days). Build the answer across the conversation, not in one message.
- Use 1-3 emojis naturally where they fit (🌴 🏝️ ✨ 🍽️ etc.) — never more than a few per message, never forced.
- Use *bold* for activity/service names, and short bullet lists (•) when listing more than one option — never a dense paragraph of options.
- Every reply must end with ONE clear, concrete next step: a specific question, or the exact name of a button/menu to tap (e.g. "Tap *Order Services* below"). Never end on a flat statement with nothing for the guest to do next.
- If a guest says "I don't understand" or repeats the same question, do NOT repeat your previous message in different words. Simplify: give the single most useful concrete fact or action, then stop.

EXAMPLES:
✅ Guest: "I have 18 days in Bali, want nature, family time, sport, and a co-working space."
✅ You: "18 days sounds amazing! 🌴 Let's build this out — what matters most for your first few days: adventure, relaxing with family, or getting settled in first?"
❌ WRONG: a 200-word single-paragraph itinerary covering every category at once, ending with no question.

✅ Guest: "How do I book?"
✅ You: "Tap *Order Services* in the menu below — you'll see everything bookable at your villa with live pricing. Want me to point you to a specific service?"
❌ WRONG: repeating "you can use the Order Services menu" inside another full paragraph identical to your last message.

✅ Guest: "Can you book me a surfing lesson?"
✅ You: "Surfing lessons aren't something we book directly, but I can sort your transport there and back through *Order Services* 🏄. Want me to look into a few good spots?"
❌ WRONG: "I'll arrange that for you" / a long explanation instead of a short, warm redirect. """


def _format_conversation_history(history: list) -> str:
    """Format conversation history for better context."""
    if not history:
        return "(First message from guest)"
    
    formatted = []
    for msg in history[-6:]:  # Last 6 messages for context
        role = "Guest" if msg["role"] == "user" else "You"
        formatted.append(f"{role}: {msg['content']}")
    
    return "\n".join(formatted)


def _clean_ai_response(text: str) -> str:
    """Remove AI artifacts and formatting issues."""
    text = text.strip()
    
    # Remove surrounding quotes
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    
    # Remove common AI prefixes
    prefixes = ["Here's my response:", "My response:", "Response:", "Here's what I'd say:", "I'd say:"]
    for prefix in prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    
    return text


async def whatsapp_response(query: str, user_id: str, villa_code: str = "WEB_VILLA_01") -> Dict[str, Any]:
    try:
        from app.services.menu_services import get_villa_info_by_code
        from app.db.session import db as _db
        from app.services.guest_service import get_guest_context_by_phone
        import re

        # Normalize user_id
        user_id = re.sub(r"\s+", "", user_id)

        # ── Kill switch + daily budget check ────────────────────────────────────
        try:
            await ai_budget_guard.check("chat", estimated_input_chars=len(query))
        except BudgetExceeded as _be:
            logger.warning(f"[ai-budget] whatsapp_response blocked for user={user_id}: {_be.reason}")
            await ai_budget_guard.record("chat", blocked=True, block_reason=_be.reason,
                                         villa_code=villa_code, user_id=user_id)
            return {
                "text": "We're sorry, but we're currently experiencing a temporary issue and are unable to process your request at the moment. Please try again in a few minutes. If the issue persists, please contact our support team at +62 851-908-28581.",
                "image_url": None,
                "should_send_menu": False,
                "menu_data": None,
                "intent": {},
                "requirements": {},
                "service_check": None,
            }

        # ── Test-sender cost guard ──────────────────────────────────────────────
        # Skip all OpenAI work (concierge gpt-4o + intelligent_service_check) for
        # test/simulation senders. WhatsApp CI/Playwright runs use test_/sim_ ids
        # and hit the live backend; without this every run burns real credits.
        if user_id and user_id.lower().startswith(("test_", "sim_")):
            return {
                "text": "Test mode — AI response skipped (no OpenAI call made).",
                "image_url": None,
                "should_send_menu": False,
                "menu_data": None,
                "intent": {},
                "requirements": {},
                "service_check": None,
            }

        # Fetch guest context
        guest_context = await get_guest_context_by_phone(user_id)
        guest_name = "Guest"
        stay_status = "unknown"
        
        if guest_context:
            guest_name = guest_context.get("full_name", "Guest")
            stay_status = guest_context.get("stay_status", "unknown")
            # WCR-VILLA-MISMATCH-01 (2026-08-24): only fall back to the
            # registration-time villa_code (guest_profile_collection, via
            # get_guest_context_by_phone) when the caller did not already
            # resolve one. The caller passes user_villa_code (villa_code_collection
            # -- QR scan / manual entry, the freshest source per
            # resolve_customer_context's documented priority) whenever it has
            # one; unconditionally overriding that with a possibly-stale
            # registration-time value caused the AI to report the WRONG villa
            # to a guest who had since re-scanned/re-entered a different villa
            # (reported live: guest asked "what is the name of my villa?" and
            # the AI answered their old registration villa, not the one they
            # had just confirmed).
            if (not villa_code or villa_code == "WEB_VILLA_01") and guest_context.get("villa_code"):
                villa_code = guest_context.get("villa_code")

        villa_info = await get_villa_info_by_code(villa_code)
        villa_context = "VILLA: GINI Bali"
        if villa_info:
             villa_context = f"VILLA: {villa_info.get('name', 'GINI Bali')} in {villa_info.get('location', 'Bali')}. Address: {villa_info.get('address', 'N/A')}"
             if villa_info.get('directions'):
                 villa_context += f". Directions: {villa_info.get('directions')}"

        # ─── Direct villa FAQ injection (MongoDB) ────────────────────────────────
        # Bypasses Pinecone similarity threshold entirely — house rules (WiFi, pool hours,
        # check-in codes, etc.) must always be answered precisely, not via fuzzy search.
        villa_rules_context = ""
        try:
            _faq_codes = [villa_code]
            if villa_code != "WEB_VILLA_01":
                _faq_codes.append("WEB_VILLA_01")
            _faq_col = _db["villa_faqs"]
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
            logger.warning(f"Villa FAQ direct lookup failed (non-fatal): {_faq_err}")

        # ─── General Knowledge Base injection (MongoDB) ──────────────────────────
        # Free-form behaviour/response guidelines, scoped global + villa-specific.
        # Appended to villa_rules_context so it flows into the same prompt paths.
        # Non-fatal — a failure here must never block the AI response.
        try:
            _kb_col = _db["knowledge_base"]
            _kb_codes = [villa_code]
            if villa_code != "WEB_VILLA_01":
                _kb_codes.append("WEB_VILLA_01")
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

        # Build guest info for AI
        guest_info_context = f"\n\nGUEST INFO:\n- Name: {guest_name}\n- Stay Status: {stay_status}\n"
        if stay_status == "pre-arrival":
             guest_info_context += "- Note: The guest hasn't checked in yet. Focus on pre-arrival arrangements, transport, and excitement!"
        elif stay_status == "active":
             guest_info_context += "- Note: The guest is currently at the villa. Focus on in-villa services, amenities, and immediate comfort."
        elif stay_status == "post-stay":
             guest_info_context += "- Note: The guest has already checked out. Focus on feedback, future bookings, and fond memories."

        # ─── Active order status injection ───────────────────────────────────────
        # Without this, "any update?" on a placed order gets a generic AI reply.
        # Feed the guest's latest pending order + the exact status wording so the
        # AI answers correctly. Non-fatal — a failure must never block the reply.
        try:
            from app.services.order_summary import get_active_order_context
            guest_info_context += await get_active_order_context(user_id)
        except Exception as _ord_err:
            logger.warning(f"Order status injection failed (non-fatal): {_ord_err}")

        # ─── AMENITY REQUEST DETECTION (QR guests only — villa_code is set) ────────
        # Intercept requests for physical amenities (towels, coffee, toiletries, ice)
        # before the general service check to handle them as direct requests.
        # AMR-VG1: concrete physical items only — generic verbs (send, need, bring,
        # more, extra, refresh) were causing false positives on ordering phrases.
        _amenity_keywords = [
            "towel", "towels", "coffee", "tea", "water", "ice", "toiletries", "shampoo",
            "soap", "conditioner", "blanket", "pillow", "toilet paper", "tissue",
            "amenities", "amenity", "refill", "toothbrush", "toothpaste", "razor", "slippers",
            "hangers", "ironing", "iron", "hair dryer", "hair-dryer", "batteries",
        ]
        _query_lower = query.lower()
        # AMR-VG2: skip amenity detection when the guest is ordering a service
        _AMENITY_SKIP_PHRASES = {"order service", "order services", "browse service", "book a service"}
        _is_amenity_request = (
            villa_code and villa_code != "WEB_VILLA_01"
            and not any(p in _query_lower for p in _AMENITY_SKIP_PHRASES)
            and any(re.search(r'\b' + re.escape(kw) + r'\b', _query_lower) for kw in _amenity_keywords)
            and len(query.strip().split()) >= 2  # At least 2 words
        )
        if _is_amenity_request:
            try:
                from app.db.session import amenities_collection as _am_col
                from datetime import datetime as _dt_am
                _now_am = _dt_am.utcnow()
                _amenity_doc = {
                    "sender_id": user_id,
                    "villa_code": villa_code,
                    "request_description": query,
                    "item_type": next((kw.title() for kw in _amenity_keywords if re.search(r'\b' + re.escape(kw) + r'\b', _query_lower)), "General"),
                    "quantity": 1,
                    "source": "whatsapp",
                    "urgency": "normal",
                    "status": "open",
                    "history": [{"status": "open", "timestamp": _now_am, "note": "Request received via WhatsApp"}],
                    "created_at": _now_am,
                    "updated_at": _now_am,
                }
                await _am_col.insert_one(_amenity_doc)
                # AMR-VG3: Notify villa manager via WhatsApp (non-fatal, lazy import avoids circular dep)
                try:
                    from app.utils.whatsapp_func import get_villa_whatsapp_by_code as _gvwbc
                    from app.utils.whatsapp_func import send_whatsapp_message as _swa_am
                    _vm_phone = await _gvwbc(villa_code)
                    if _vm_phone:
                        _ts_am = _now_am.strftime("%d %b %Y, %H:%M UTC")
                        await _swa_am(
                            _vm_phone,
                            f"🛎️ *New Amenity Request (WhatsApp)*\n\n"
                            f"• *Guest:* {user_id}\n"
                            f"• *Villa:* {villa_code}\n"
                            f"• *Request:* {query}\n"
                            f"• *Item:* {_amenity_doc['item_type']}\n"
                            f"• *Time:* {_ts_am}\n\n"
                            f"Please fulfil this request and update the status in the Host Interface."
                        )
                    else:
                        # VM-NOTIFY-LOG-PARITY-01 (2026-09-04): this branch
                        # previously had no log line at all on a missing VM
                        # phone — every other VM-notify call site (amenity
                        # website, passport both channels, maintenance both
                        # channels) logs a WARNING here, leaving zero debug
                        # trail for this one path when "VM didn't get
                        # notified" needs investigating later.
                        logger.warning(f"[Amenity WA notify] no villa WhatsApp number found for {villa_code} — staff not notified")
                except Exception as _vm_notify_err:
                    logger.warning(f"[Amenity WA notify] non-fatal: {_vm_notify_err}")
                _amenity_resp = (
                    f"🛎️ Got it! We've noted your request for *{_amenity_doc['item_type']}* and our team will bring it to your room shortly.\n\n"
                    f"_Your request is now open and being tracked. You'll receive an update when it's on the way!_ ✅"
                )
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", _amenity_resp)
                return {
                    "text": _amenity_resp,
                    "image_url": None,
                    "should_send_menu": False,
                    "menu_data": None,
                    "intent": {},
                    "requirements": {},
                    "service_check": None
                }
            except Exception as _am_err:
                logger.warning(f"Amenity DB save failed (non-fatal): {_am_err}")

        # ─── LOCAL LANGUAGE LESSON ──────────────────────────────────────────────
        _is_lang_lesson = "local language lesson" in _query_lower or _query_lower in ["next word", "belajar lagi"]
        if _is_lang_lesson:
            from app.services.menu_services import cache
            df = cache.get("language_lesson_df")
            if df is not None and not df.empty:
                row = df.sample(1).iloc[0]
                eng = row.get("English", "")
                indo = row.get("Indonesian", "")
                indo_pron = row.get("Indonesian Pronunciation", "")
                bali = row.get("Balinese", "")
                bali_pron = row.get("Balinese Pronunciation", "")
                ctx = row.get("Cultural Context", "")

                if "local language lesson" in _query_lower:
                    intro = "Hi! Ready for your first language lesson of the day? 🎉\nOr feel free to ask us about any word or phrase you're curious about – We're happy to help you with that too! 😊\n\n"
                else:
                    intro = "Awesome! Here’s your next word. 🌟\n\n"
                    
                resp = (
                     f"{intro}"
                     f"📖 *Today's Word:* {eng}\n"
                     f"🇮🇩 *Indonesian:* {indo} (_{indo_pron}_)\n"
                     f"🛕 *Balinese:* {bali} (_{bali_pron}_)\n\n"
                     f"💡 *Example/Usage:* {ctx}\n\n"
                     f"Reply *Next Word* to learn the next one, or *Stop* to return to the menu! 🌴"
                )
                save_message(user_id, "user", query)
                save_message(user_id, "assistant", resp)
                return {
                    "text": resp,
                    "image_url": None,
                    "should_send_menu": False,
                    "menu_data": None,
                    "intent": {},
                    "requirements": {},
                    "service_check": None
                }
                
        # Handle "Stop" for language lesson
        chat_history_full = get_conversation_history(user_id)
        if _query_lower == "stop" and len(chat_history_full) > 0 and any("next word" in str(m.get('content', '')).lower() for m in reversed(chat_history_full[-4:])):
            txt = "You did great today! 🌟 Feel free to pick another topic from the menu when you're ready."
            save_message(user_id, "user", query)
            save_message(user_id, "assistant", txt)
            return {
                "text": txt,
                "image_url": None,
                "should_send_menu": False,
                "menu_data": None,
                "intent": {},
                "requirements": {},
                "service_check": None
            }

        service_check = await ai_menu_generator.intelligent_service_check(query)
    
        intent: Optional[Dict] = None
        requirements: Dict = ai_menu_generator.extract_requirements(query) or {}
        
        if service_check.get("we_offer_it", False):
            intent = ai_menu_generator.detect_service_intent(query)

            matched = service_check.get("matched_service")
            if not intent and matched:
                for service_type, info in ai_menu_generator.service_categories.items():
                    if info.get("subcategory") == matched:
                        intent = {
                            "service_type": service_type,
                            "category": info.get("category", ""),
                            "subcategory": info.get("subcategory", "")
                        }
                        break

        # WCR-23 (additive — does not alter the block above): deterministic backstop
        # for intelligent_service_check() false negatives. That call is a live AI
        # classification and is not 100% reliable — it can say we_offer_it=False for
        # a service explicitly listed in its own AVAILABLE SERVICES prompt (reported:
        # "can I order a balinese massage" declined even though Massage/Balinese
        # Massage - 60min is a real, priced, Seminyak-available catalog item).
        # detect_service_intent() is a deterministic keyword matcher and is far more
        # reliable for well-known service names, but was previously only ever called
        # inside the block above — i.e. only when the AI had ALREADY said yes, so it
        # could never correct a false negative. This runs only in the complementary
        # case (AI said no, no intent yet) and, if the deterministic matcher finds a
        # confident keyword match, overrides the AI's judgement. Placed BEFORE the
        # WCR-16 location guard below so that guard still gets full authority to
        # decline again if the matched service genuinely isn't available at this
        # guest's villa zone — this backstop does not bypass that protection.
        if not intent and not service_check.get("we_offer_it", False):
            _wcr23_backstop = ai_menu_generator.detect_service_intent(query)
            if _wcr23_backstop:
                intent = _wcr23_backstop
                service_check = dict(service_check)
                service_check["we_offer_it"] = True
                service_check["matched_service"] = _wcr23_backstop.get("subcategory")

        # WCR-16: Location availability guard — a service may be in our global catalog
        # but unavailable at this villa's zone (e.g. Laundry has 0-price for V1/Seminyak
        # in Prices Set).  When that is the case, force we_offer_it=False and intent=None
        # so the system routes to PATH 2 (graceful decline + villa-specific alternatives)
        # instead of PATH 1 (show menu with wrong/zero price + contradictory AI text).
        if intent and villa_code and villa_code not in ("WEB_VILLA_01", ""):
            _loc_sub = (intent.get("subcategory") or "").lower().strip()
            if _loc_sub:
                try:
                    from app.services.menu_services import _get_available_sets_for_zone, get_villa_location_by_code as _gvlbc2
                    _loc_zone = await _gvlbc2(villa_code)
                    if _loc_zone:
                        _, _loc_avail_subs = _get_available_sets_for_zone(_loc_zone.lower())
                        if _loc_avail_subs and _loc_sub not in _loc_avail_subs:
                            print(f"[loc_avail_guard] '{_loc_sub}' unavailable at {villa_code}/{_loc_zone} — routing to decline path")
                            service_check = dict(service_check)
                            service_check["we_offer_it"] = False
                            service_check["not_at_location"] = True
                            intent = None
                except Exception as _lag2:
                    print(f"[loc_avail_guard] non-fatal: {_lag2}")

        # Build intent info string
        intent_info = ""
        if intent and isinstance(intent, dict) and intent.get("category"):
            budget_str = (
                f"Under IDR {int(requirements.get('budget') or 0):,}"
                if requirements.get("budget")
                else "Any budget"
            )
            intent_info = f"""
DETECTED INTENT:
- Service: {intent.get('service_type', 'Unknown')}
- Category: {intent.get('category')}
- Subcategory: {intent.get('subcategory', 'Not specified')}
- Location: {requirements.get('location', 'Any area in Bali')}
- People: {requirements.get('people_count', 'Any')}
- Budget: {budget_str}
- Time: {requirements.get('time_preference', 'Flexible')}
""".strip()

        # Get chat history
        chat_history = get_conversation_history(user_id)
        conversation = trim_history(chat_history + [{"role": "user", "content": query}])

        # Get RAG context
        context = await rag_service.get_rag_context(query, "general", villa_code)

        # ── Service catalog injection for "what services do you have?" queries ──
        # Replace RAG context with the live Sheets catalog so the AI can only
        # recommend services that actually exist in the Google Sheets source of truth.
        _q_lower_wa = query.lower()
        _CATALOG_PHRASES_WA = [
            "what services", "services do you", "services you offer",
            "available services", "services available", "what can i book",
            "what can we book", "what do you offer", "what do you have",
            "list of services", "all services", "show me your service",
            "what can you recommend", "recommend some service",
            "what activities do you", "what experiences do you",
            "what options do you", "what can i order",
            # Natural discovery phrasing — mirror web Point 18 so WhatsApp grounds
            # "do you know any massage around here?" in the catalog instead of a stub.
            "where can i order", "where can i get", "where can i book",
            "where do i order", "where to order", "where to get",
            "can i order", "can i get", "can i book", "do you know",
            "any recommendation", "recommend", "suggestion", "suggest",
            "around here", "near me", "nearby", "any good", "looking for",
            "is there any", "are there any", "i want to book", "i want to order",
            # Shisha / hookah: without these the catalog injection
            # does not fire and the AI hallucinates from RAG.
            "shisha", "hookah", "shisha party", "hookah party",
        ]
        if any(p in _q_lower_wa for p in _CATALOG_PHRASES_WA):
            try:
                from app.services.menu_services import get_service_catalog_context
                _catalog_ctx = await get_service_catalog_context(villa_code=villa_code)
                if _catalog_ctx:
                    context = _catalog_ctx
            except Exception as _wac_err:
                print(f"[WA catalog context] non-fatal: {_wac_err}")

        # ── D&P grounding for free-text discount/promotion questions (WCR parity) ──
        # Root cause fixed (2026-08-15, Adam/Clay live report): free-text D&P
        # questions had zero grounding on WhatsApp either — the AI would guess
        # and incorrectly claim no promotions exist even when real, active ones
        # did (confirmed live: /dnp/categories returned 5 real active categories
        # while the AI told the guest it had no information). Channel-parity
        # mirror of the same fix in ai_prompt.py.
        _DNP_PHRASES_WA = [
            "discount", "discounts", "promotion", "promotions", "promo",
            "promos", "deal", "deals", "voucher", "vouchers", "offer",
            "offers", "special offer", "any discount", "any promo",
        ]
        if any(p in _q_lower_wa for p in _DNP_PHRASES_WA):
            try:
                from app.services.menu_services import get_dnp_context
                _dnp_ctx_wa = await get_dnp_context()
                if _dnp_ctx_wa:
                    context = _dnp_ctx_wa
            except Exception as _wad_err:
                print(f"[WA D&P context] non-fatal: {_wad_err}")

        will_show_menu = bool(intent and isinstance(intent, dict) and intent.get("category"))
        will_decline_service = (
            service_check.get("is_service_request", False) and 
            not service_check.get("we_offer_it", False) and
            service_check.get("confidence", 0) > 0.7
        )

        # ============================================================
        # RESPONSE PATH 1: Show menu (we have this service)
        # ============================================================
        if will_show_menu:
            # Ground the reply in the LIVE service catalog so the guest gets ACTUAL
            # services + prices + an offer to book — NOT a bare "check out our
            # services below" stub. The stub dead-ended for guests when the menu
            # card didn't render (generate_service_menu returned nothing), leaving
            # only the useless text. Reported by Clay/Adam 2026-07-11.
            _wa_catalog_for_menu = context
            try:
                from app.services.menu_services import get_service_catalog_context as _gscc_menu
                _wa_full_catalog = await _gscc_menu(villa_code=villa_code)
                if _wa_full_catalog:
                    _wa_catalog_for_menu = _wa_full_catalog
            except Exception as _wa_cat_err:
                print(f"[whatsapp_response] catalog context for menu path failed (non-fatal): {_wa_cat_err}")

            _wa_interest = (intent.get("subcategory") or intent.get("category") or "our services") if intent else "our services"

            prompt = f"""You are GINI Bali's service concierge for guests at {villa_context}.{guest_info_context}
{f"{villa_rules_context}" + chr(10) + chr(10) if villa_rules_context else ""}
AVAILABLE EASYBALI SERVICES (the ONLY services and prices you may mention — use these exact names and prices):
{_wa_catalog_for_menu if _wa_catalog_for_menu else f"No catalog data found for {villa_code}."}

Recent conversation:
{conversation[-5:]}

GUEST'S MESSAGE: "{query}"

INSTRUCTIONS:
- The guest is interested in {_wa_interest}. Reply warmly and conversationally (WhatsApp-friendly, under 90 words).
- Recommend the 2-3 most relevant options from the list above, EACH WITH ITS PRICE (IDR).
- Use ONLY services and prices from the list above — never invent a service or a price.
- End by offering to book, e.g. "Want me to set one up? Just tap *Order Services* below, or tell me which one you'd like."

Now reply:"""

            temperature = 0.5
            max_tokens = 220

        # ============================================================
        # RESPONSE PATH 2: Gracefully decline (unavailable service)
        #
        # PATH 2a — service exists in global catalog but NOT at this villa's zone.
        #   Inject the live location-filtered catalog so the AI knows what IS
        #   available here. Response is purely informational: acknowledge the gap,
        #   suggest the villa manager for the missing service, list what we DO have.
        #   No booking CTA, no "tap Order Services", no WhatsApp Flow triggered.
        #
        # PATH 2b — service is genuinely not in GINI Bali's catalog at all.
        #   Standard graceful decline; suggest genuinely related alternatives only.
        # ============================================================
        elif will_decline_service:
            conversation_context = _format_conversation_history(conversation)
            requested_service_name = service_check.get("requested_service", "that service")

            if service_check.get("not_at_location"):
                # PATH 2a: In catalog globally but unavailable at this villa's zone.
                # Pull the live, location-filtered catalog so the AI can tell the
                # guest what services we DO offer here right now.
                _p2a_catalog = context
                try:
                    from app.services.menu_services import get_service_catalog_context as _gscc_2a
                    _live_2a = await _gscc_2a(villa_code=villa_code)
                    if _live_2a:
                        _p2a_catalog = _live_2a
                except Exception as _p2a_err:
                    print(f"[PATH2a] catalog fetch non-fatal: {_p2a_err}")

                prompt = f"""You are GINI Bali's friendly concierge for guests at {villa_context}.{guest_info_context}

SERVICES AVAILABLE AT THIS VILLA RIGHT NOW:
{_p2a_catalog if _p2a_catalog else f"(catalog unavailable for {villa_code})"}

GUEST'S MESSAGE: "{query}"

The guest is asking about {requested_service_name}. This service is NOT currently available at their villa.

YOUR TASK — keep it warm, honest and SHORT (60-80 words):
1. Tell the guest that {requested_service_name} isn't available at their villa right now.
2. Suggest they contact their villa manager if they need this arranged locally.
3. From the SERVICES AVAILABLE list above, casually mention 2-3 things we DO offer that might interest them — use exact names from the list.
4. DO NOT say "tap Order Services", DO NOT prompt them to book anything. This is information only.
5. DO NOT invent services or prices — only use what is in the SERVICES AVAILABLE list above.

Reply naturally, like a helpful friend. No booking links, no flow prompts."""

                temperature = 0.5
                max_tokens = 160

            else:
                # PATH 2b: Service genuinely not in GINI Bali's catalog at all.
                prompt = f"""{EASYBALI_CORE_IDENTITY}{guest_info_context}

CURRENT CONTEXT: You are assisting a guest at {villa_context}.
{f"{villa_rules_context}" + chr(10) + chr(10) if villa_rules_context else ""}
KNOWLEDGE BASE CONTEXT (PRIMARY SOURCE for {villa_code}):
{context if context else "Note: No specific spreadsheet data found for this query."}

CONVERSATION HISTORY:
{conversation_context}

GUEST'S CURRENT MESSAGE: "{query}"

**STRICT INSTRUCTION**: The guest is asking about **{requested_service_name}**, which GINI Bali does NOT offer and CANNOT book.

YOUR TASK:
1. Use the KNOWLEDGE BASE CONTEXT above to confirm what we DO and DON'T offer.
2. Warmly acknowledge their interest in {requested_service_name}.
3. Be clear and honest: GINI Bali doesn't book this — do NOT say "I'll arrange it" or imply any booking action.
4. Only suggest alternatives that are genuinely related to what they asked for. Do NOT suggest unrelated services (e.g. don't suggest massage if they asked about laundry or cleaning).
5. If no genuinely related alternative exists in the context, recommend they ask their villa manager for a local referral.
6. End with an invitation to ask about what GINI Bali CAN help with.

NEVER imply you will book, arrange, or confirm any external service.
Keep response 80-120 words. Warm, specific, real GINI Bali services only.

YOUR RESPONSE:"""

                temperature = 0.8
                max_tokens = 180

        # ============================================================
        # RESPONSE PATH 3: Natural conversation (not service-related)
        # ============================================================
        else:
            conversation_context = _format_conversation_history(conversation)
            
            prompt = f"""{EASYBALI_CORE_IDENTITY}{guest_info_context}

CURRENT CONTEXT: You are assisting a guest at {villa_context}.
{f"{villa_rules_context}" + chr(10) + chr(10) if villa_rules_context else ""}
KNOWLEDGE BASE CONTEXT (MANDATORY PRIMARY SOURCE for {villa_code}):
{context if context else f"Note: No specific spreadsheet data found for {villa_code}. Use internal knowledge but stay aligned with GINI Bali standards for this villa."}

CONVERSATION HISTORY:
{conversation_context}

GUEST'S CURRENT MESSAGE: "{query}"

STRICT RULES:
1. **GROUNDING RULE**: Always check the 'KNOWLEDGE BASE CONTEXT' first. If the answer is there, use it as the absolute source of truth.
2. **FALLBACK RULE**: Only use your internal knowledge and reasoning if the 'KNOWLEDGE BASE CONTEXT' is silent on the topic.
3. **NO HALLUCINATION**: Do not invent services, prices, or villa-specific rules that are not in the context. If data is missing, admit you don't have the exact detail but offer general helpful advice.
4. **NO FAKE BOOKINGS**: Never say you will book, arrange, confirm, or process anything for the guest unless it is a service directly in the GINI Bali catalog. For external activities (tours, restaurants, rafting, surfing, etc.), you can share general information but must be clear: "That's something you'd book directly / ask your villa manager about."
5. Respond naturally, paint experiences, and be solution-oriented.
6. Keep response 50-100 words.

YOUR RESPONSE:"""

            temperature = 0.5  # Lowered from 0.8 to prevent erratic behavior
            max_tokens = 150

        # ========================================
        # Generate response
        # ========================================
        completion = await client.chat.completions.create(
            model=settings.OPENAI_MODEL_NAME,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": query}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            presence_penalty=0.1,  # Lowered from 0.3 for stability
            frequency_penalty=0.1  # Lowered from 0.3 for stability
        )
        _wa_usage = getattr(completion, "usage", None)
        await ai_budget_guard.record(
            "chat",
            input_tokens=getattr(_wa_usage, "prompt_tokens", 0),
            output_tokens=getattr(_wa_usage, "completion_tokens", 0),
            model=settings.OPENAI_MODEL_NAME,
            villa_code=villa_code,
            user_id=user_id,
        )
        ai_text = completion.choices[0].message.content.strip()
        ai_text = _clean_ai_response(ai_text)

        save_message(user_id, "user", query)
        save_message(user_id, "assistant", ai_text)

        # ========================================
        # Generate menu only if we have the service
        # ========================================
        menu_data = None
        should_send_menu = False
        image_url = None

        if intent and isinstance(intent, dict) and intent.get("category"):
            try:
                menu_data = await ai_menu_generator.generate_service_menu(
                    category=intent.get("category", ""),
                    subcategory=intent.get("subcategory"),
                    requirements=requirements,
                )  # WCR-13b: _villa_code param unused; villa_code= kwarg caused silent TypeError
                should_send_menu = bool(menu_data and menu_data.get("sections"))
                
                if menu_data:
                    image_url = menu_data.get("image_url")
            except Exception as menu_err:
                print(f"[whatsapp_response] Menu generation error (non-fatal): {menu_err}")

        # ── Catalog-match menu fallback ───────────────────────────────────────
        # Runs only when the existing intent-based path produced no menu AND the
        # AI confirmed we offer the requested service.  Builds the booking menu
        # directly from the Google Sheets catalog so the guest always sees all
        # matching options and can tap to place an order without extra steps.
        # Additive layer — zero changes to existing intent / menu-gen logic.
        if not should_send_menu and service_check and service_check.get("we_offer_it") and service_check.get("matched_service"):
            try:
                from app.services.google_sheets_service import google_sheets_service
                from app.utils.formatters import clean_price_string
                _all_svcs = await google_sheets_service.get_services_data()
                _matched_name = service_check["matched_service"].lower().strip()
                _cat_rows = []
                _detected_cat = None
                for _svc in (_all_svcs or []):
                    _svc_sub  = _svc.get("subcategory", "").lower().strip()
                    _svc_name = _svc.get("service_name", "").lower().strip()
                    # Match if the matched_service appears in the subcategory name or vice-versa
                    if (_matched_name in _svc_sub or _svc_sub in _matched_name or
                            _matched_name in _svc_name):
                        _cat_rows.append(_svc)
                        if not _detected_cat:
                            _detected_cat = _svc.get("category", "")
                if _cat_rows and _detected_cat:
                    _menu_rows = []
                    for _idx, _sv in enumerate(_cat_rows[:15]):
                        _nm = _sv.get("service_name", "Unknown")
                        _pr = clean_price_string(_sv.get("price", ""))
                        _menu_rows.append({
                            "id": f"ai_catalog_{_idx}_{_nm.replace(' ', '_')}",
                            "title": _nm,
                            "full_title": _nm,
                            "description": _sv.get("description", "")[:72],
                            "price": _pr,
                        })
                    if _menu_rows:
                        menu_data = {
                            "title": service_check["matched_service"][:60],
                            "description": "Tap an option below to book.",
                            "image_url": None,
                            "sections": [{"title": "Available Options", "rows": _menu_rows}],
                        }
                        should_send_menu = True
            except Exception as _cfb_err:
                print(f"[whatsapp_response] catalog menu fallback (non-fatal): {_cfb_err}")

        return {
            "text": ai_text,
            "image_url": image_url,
            "should_send_menu": should_send_menu,
            "menu_data": menu_data,
            "intent": intent or {},
            "requirements": requirements,
            "service_check": service_check
        }

    except Exception as e:
        print(f"[whatsapp_response] Critical error: {e}")
        traceback.print_exc()
        
        return {
            "text": "Hi there! I'm your GINI Bali concierge — here to help with in-villa spa & massage, private dining, transport, tours, and everything to make your stay incredible. What would you like to arrange today?",
            "image_url": None,
            "should_send_menu": False,
            "menu_data": None,
            "intent": {},
            "requirements": {},
            "service_check": None
        }