"""
WhatsApp Order Services Onboarding — isolated module, zero-regression design.

Architecture change (2026-06-17): This module no longer intercepts all messages.
It is triggered in two ways only:
  1. Session continuation — called when has_active_session() returns True (mid-flow user)
  2. Order Services entry   — called with start_if_new=True when user taps Order Services
                              and has no villa code

Collecting:
  Step LOCATION  — numbered zone list; user picks their area
  Step VILLA     — numbered villa list for chosen zone; user picks their villa
  Step NAME      — "First Last"; creates full_name
  Step CHECKIN   — check-in date (natural language, parsed via dateutil)
  Step CHECKOUT  — check-out date → on success, creates GuestProfile + saves villa code

Returns True  → message handled here; caller must return immediately.
Returns False → sender already set up OR not mid-flow; caller continues unchanged.

Session state: in-memory _sessions dict (short-lived, ~5 minutes typical).
Limitation: sessions reset on Render restart; rare edge case (guest retypes their zone).

Circular-import design:
  send_whatsapp_message imported lazily inside _send()
  _is_sender_sp imported lazily inside _is_service_provider()
  Both prevent circular dependencies with whatsapp_func.py.
"""

import datetime
import logging

from app.db.session import villa_code_collection, guest_profile_collection
from app.services.menu_services import get_available_zones, get_all_villas

logger = logging.getLogger(__name__)

# {sender_id: {"step": "LOCATION"|"VILLA"|"NAME"|"CHECKIN"|"CHECKOUT",
#              "zones": list, "location": str, "villas": list,
#              "villa_code": str, "villa_name": str, "full_name": str, "check_in": str}}
_sessions: dict = {}


# ── Public helpers ────────────────────────────────────────────────────────────

def has_active_session(sender_id: str) -> bool:
    """Synchronous check — True if sender is mid-flow. Used by whatsapp_func.py."""
    return sender_id in _sessions


# ── Private helpers ───────────────────────────────────────────────────────────

async def _get_villa_code(sender_id: str):
    try:
        doc = await villa_code_collection.find_one({"sender_id": sender_id})
        return doc.get("villa_code") if doc else None
    except Exception:
        return None


async def _is_service_provider(sender_id: str) -> bool:
    """SPs have their own flow — bypass onboarding entirely.
    Delegates to the canonical _is_sender_sp in whatsapp_func via lazy import
    so that test mocks on app.utils.whatsapp_func._is_sender_sp work correctly."""
    try:
        from app.utils.whatsapp_func import _is_sender_sp
        return await _is_sender_sp(sender_id)
    except Exception:
        return False


async def _save_villa_code(sender_id: str, villa_code: str, villa_name: str, location: str):
    try:
        now = datetime.datetime.now()
        await villa_code_collection.update_one(
            {"sender_id": sender_id},
            {
                "$set": {
                    "sender_id": sender_id,
                    "villa_code": villa_code,
                    "villa_name": villa_name,
                    "location": location,
                    "source": "manual_selection",
                    "verified_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    except Exception as e:
        logger.error(f"[villa_onboarding] Failed to save villa code for {sender_id}: {e}")


async def _save_guest_profile(
    sender_id: str, full_name: str, check_in: str, check_out: str,
    villa_code: str, villa_name: str, location: str
):
    """Create or update a GuestProfile after all steps are complete."""
    try:
        # Canonical phone normalisation — MUST match resolve_customer_context so a
        # guest's profile is keyed identically across web, WhatsApp, and onboarding.
        # The old logic prepended '62' to any number not starting with 62, which
        # mangled non-Indonesian numbers (e.g. UAE 971… -> 62971…) and fragmented
        # their profile. _normalise_phone is digits-only with no country assumption.
        from app.services.customer_context import _normalise_phone
        _phone = _normalise_phone(sender_id) or sender_id

        now = datetime.datetime.now(datetime.timezone.utc)
        import uuid as _uuid
        await guest_profile_collection.update_one(
            {"$or": [{"phone_number": sender_id}, {"phone_number": _phone}]},
            {
                "$set": {
                    "full_name": full_name,
                    "phone_number": _phone,
                    "sender_id": sender_id,
                    "villa_code": villa_code,
                    "villa_name": villa_name,
                    "location_zone": location,
                    "check_in_date": check_in,
                    "check_out_date": check_out,
                    "source": "whatsapp",
                    "last_active_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "guest_id": str(_uuid.uuid4()),
                    "created_at": now,
                },
            },
            upsert=True,
        )
    except Exception as e:
        logger.error(f"[villa_onboarding] Failed to save guest profile for {sender_id}: {e}")


async def _send(sender_id: str, text: str):
    # Lazy import avoids circular dependency with whatsapp_func.py
    from app.utils.whatsapp_func import send_whatsapp_message
    await send_whatsapp_message(sender_id, text)


def _extract_text(message_payload: dict):
    if "text" in message_payload:
        return message_payload["text"]["body"].strip()
    if "interactive" in message_payload:
        interactive = message_payload["interactive"]
        if interactive.get("type") == "button_reply":
            return interactive["button_reply"].get("title", "").strip()
        if interactive.get("type") == "list_reply":
            return interactive["list_reply"].get("title", "").strip()
    return None


def _parse_date(text: str) -> str | None:
    """Parse a natural-language date string. Returns ISO 'YYYY-MM-DD' or None."""
    try:
        from dateutil import parser as _dp
        # Default to current year if not provided
        default = datetime.datetime(datetime.datetime.now().year, 1, 1)
        parsed = _dp.parse(text, default=default, dayfirst=True)
        return parsed.strftime("%Y-%m-%d")
    except Exception:
        return None


def _fmt_date(iso: str) -> str:
    """Format ISO date as '20 Jun 2026' for display."""
    try:
        d = datetime.datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%-d %b %Y")
    except Exception:
        return iso


# ── Step handlers ─────────────────────────────────────────────────────────────

async def _start_onboarding(sender_id: str) -> bool:
    zones = await get_available_zones()
    if not zones:
        logger.warning(f"[villa_onboarding] No zones available, skipping for {sender_id}")
        return False

    lines = [
        "*Welcome to GINI Bali!* 🌴",
        "",
        "To show you the right services for your stay, I need to know which area your villa is in.",
        "",
        "Reply with the *number* of your area:",
    ]
    for i, zone in enumerate(zones, 1):
        lines.append(f"{i}. {zone}")
    lines.append("\n0. My area isn't listed — get help")

    _sessions[sender_id] = {"step": "LOCATION", "zones": zones}
    await _send(sender_id, "\n".join(lines))
    return True


async def _handle_location_step(sender_id: str, session: dict, text: str) -> bool:
    zones = session.get("zones", [])

    # Guard: If user says "no", "I don't know", etc., launch WhatsApp Flow UI
    # instead of continuing with text-based numbered selection
    _no_words = {"no", "nope", "nah", "don't know", "dont know", "i don't know",
                 "i dont know", "not sure", "no idea", "idk", "dunno"}
    if text.lower() in _no_words or text.lower().startswith("no "):
        logger.critical(f"VILLA_ONBOARDING_FLOW_UI_GUARD_TRIGGERED_LOCATION_STEP sender={sender_id} text={repr(text)}")
        _sessions.pop(sender_id, None)
        # Launch WhatsApp Flow UI with hardcoded flow ID
        _no_flow_id = "1465038141489393"
        import uuid
        _no_flow_token = f"cat_{sender_id}_{uuid.uuid4().hex[:8]}"
        from app.services.whatsapp_flows_service import send_category_flow_message
        try:
            logger.info(f"Sending WhatsApp Flow UI to {sender_id} with flow_id {_no_flow_id}")
            await send_category_flow_message(sender_id, _no_flow_id, _no_flow_token)
            logger.info(f"Flow UI sent successfully to {sender_id}")
        except Exception as e:
            logger.error(f"Flow launch failed for {sender_id}: {e}")
            await _send(
                sender_id,
                "No problem! Please share your area (e.g. Seminyak, Canggu) and we'll show you the villas there."
            )
        return True

    if text == "0":
        _sessions.pop(sender_id, None)
        await _send(
            sender_id,
            "No problem! Please share your villa name and location and our team will set everything up for you.",
        )
        return True

    if text.lower() in ("hi", "hello", "menu"):
        lines = ["Please reply with a *number* to select your area:\n"]
        for i, z in enumerate(zones, 1):
            lines.append(f"{i}. {z}")
        lines.append("\n0. My area isn't listed")
        await _send(sender_id, "\n".join(lines))
        return True

    try:
        pick = int(text)
    except ValueError:
        await _send(
            sender_id,
            f"Please reply with a *number* between 1 and {len(zones)}, or *0* if your area isn't listed.",
        )
        return True

    if pick < 1 or pick > len(zones):
        await _send(
            sender_id,
            f"Please reply with a *number* between 1 and {len(zones)}, or *0* if your area isn't listed.",
        )
        return True

    chosen_zone = zones[pick - 1]
    all_villas = await get_all_villas()
    zone_villas = [
        v for v in all_villas
        if v.get("location", "").lower() == chosen_zone.lower() and v.get("code")
    ]

    if not zone_villas:
        await _send(
            sender_id,
            f"I couldn't find any villas listed in *{chosen_zone}* yet.\n\nReply *0* for help, or *back* to choose a different area.",
        )
        return True

    lines = [f"Got it — *{chosen_zone}*!\n\nWhich villa are you staying in? Reply with the *number*:\n"]
    for i, v in enumerate(zone_villas, 1):
        lines.append(f"{i}. {v['name']} ({v['code']})")
    lines.append("\n0. My villa isn't listed\nback — choose a different area")

    _sessions[sender_id] = {
        "step": "VILLA",
        "location": chosen_zone,
        "villas": zone_villas,
        "zones": zones,
    }
    await _send(sender_id, "\n".join(lines))
    return True


async def _handle_villa_step(sender_id: str, session: dict, text: str) -> bool:
    villas = session.get("villas", [])
    zones = session.get("zones", [])

    # Guard: If user says "no", "I don't know", etc., launch WhatsApp Flow UI
    _no_words = {"no", "nope", "nah", "don't know", "dont know",
                 "i don't know", "i dont know", "not sure", "no idea", "idk", "dunno"}
    if text.lower() in _no_words or text.lower().startswith("no "):
        logger.critical(f"VILLA_ONBOARDING_FLOW_UI_GUARD_TRIGGERED_VILLA_STEP sender={sender_id} text={repr(text)}")
        _sessions.pop(sender_id, None)
        # Launch WhatsApp Flow UI with hardcoded flow ID
        _no_flow_id = "1465038141489393"
        import uuid
        _no_flow_token = f"cat_{sender_id}_{uuid.uuid4().hex[:8]}"
        from app.services.whatsapp_flows_service import send_category_flow_message
        try:
            logger.info(f"Sending WhatsApp Flow UI to {sender_id} with flow_id {_no_flow_id}")
            await send_category_flow_message(sender_id, _no_flow_id, _no_flow_token)
            logger.info(f"Flow UI sent successfully to {sender_id}")
        except Exception as e:
            logger.error(f"Flow launch failed for {sender_id}: {e}")
            await _send(
                sender_id,
                "No problem! Please share your area (e.g. Seminyak, Canggu) and we'll show you the villas there."
            )
        return True

    if text.lower() in ("back", "b"):
        _sessions[sender_id] = {"step": "LOCATION", "zones": zones}
        lines = ["No problem! Which area is your villa in? Reply with the *number*:\n"]
        for i, z in enumerate(zones, 1):
            lines.append(f"{i}. {z}")
        lines.append("\n0. Not listed — get help")
        await _send(sender_id, "\n".join(lines))
        return True

    if text == "0":
        _sessions.pop(sender_id, None)
        await _send(
            sender_id,
            "No problem! Please share your villa name and our team will help set everything up for you.",
        )
        return True

    if text.lower() in ("hi", "hello", "menu"):
        lines = ["Please reply with a *number* to select your villa:\n"]
        for i, v in enumerate(villas, 1):
            lines.append(f"{i}. {v['name']} ({v['code']})")
        lines.append("\n0. My villa isn't listed\nback — choose a different area")
        await _send(sender_id, "\n".join(lines))
        return True

    try:
        pick = int(text)
    except ValueError:
        await _send(
            sender_id,
            f"Please reply with a *number* between 1 and {len(villas)}, *0* if not listed, or *back* to pick a different area.",
        )
        return True

    if pick < 1 or pick > len(villas):
        await _send(
            sender_id,
            f"Please reply with a *number* between 1 and {len(villas)}, *0* if not listed, or *back* to pick a different area.",
        )
        return True

    chosen = villas[pick - 1]
    villa_code = chosen["code"]
    villa_name = chosen["name"]
    location = session.get("location", "")

    # Save villa code immediately so it persists even if the guest abandons later steps
    await _save_villa_code(sender_id, villa_code, villa_name, location)

    _sessions[sender_id] = {
        "step": "NAME",
        "location": location,
        "villa_code": villa_code,
        "villa_name": villa_name,
        "zones": zones,
    }
    await _send(
        sender_id,
        f"*{villa_name}* — great choice! 🏡\n\n"
        "To complete your booking profile, what is your *full name*?\n"
        "_(Reply with First and Last name, e.g. *Maria Santos*)_",
    )
    return True


async def _handle_name_step(sender_id: str, session: dict, text: str) -> bool:
    # Accept any text with at least two words as a valid full name
    parts = text.strip().split()
    if len(parts) < 2:
        await _send(
            sender_id,
            "Please enter your *first and last name* — e.g. *Maria Santos*",
        )
        return True

    # Title-case and store
    full_name = " ".join(p.capitalize() for p in parts)
    _sessions[sender_id] = {**session, "step": "CHECKIN", "full_name": full_name}
    await _send(
        sender_id,
        f"Nice to meet you, *{full_name.split()[0]}*! 😊\n\n"
        "What is your *check-in date*?\n"
        "_(e.g. *20 Jun*, *20/06/2026*, or *June 20*)_",
    )
    return True


async def _handle_checkin_step(sender_id: str, session: dict, text: str) -> bool:
    iso = _parse_date(text)
    if not iso:
        await _send(
            sender_id,
            "I couldn't understand that date. Please try again — e.g. *20 Jun*, *20/06/2026*, or *June 20 2026*.",
        )
        return True

    _sessions[sender_id] = {**session, "step": "CHECKOUT", "check_in": iso}
    await _send(
        sender_id,
        f"Check-in: *{_fmt_date(iso)}* ✅\n\n"
        "What is your *check-out date*?\n"
        "_(e.g. *25 Jun*, *25/06/2026*)_",
    )
    return True


async def _handle_checkout_step(sender_id: str, session: dict, text: str) -> bool:
    iso = _parse_date(text)
    if not iso:
        await _send(
            sender_id,
            "I couldn't understand that date. Please try again — e.g. *25 Jun*, *25/06/2026*, or *June 25 2026*.",
        )
        return True

    check_in = session.get("check_in", "")
    if check_in and iso <= check_in:
        await _send(
            sender_id,
            f"Check-out must be *after* your check-in date ({_fmt_date(check_in)}). "
            "Please enter a later date.",
        )
        return True

    villa_code = session.get("villa_code", "")
    villa_name = session.get("villa_name", "")
    location = session.get("location", "")
    full_name = session.get("full_name", "")

    # Save the complete guest profile
    await _save_guest_profile(sender_id, full_name, check_in, iso, villa_code, villa_name, location)
    _sessions.pop(sender_id, None)

    await _send(
        sender_id,
        f"*You're all set!* 🎉\n\n"
        f"📍 *Villa:* {villa_name} ({villa_code}), {location}\n"
        f"👤 *Name:* {full_name}\n"
        f"📅 *Stay:* {_fmt_date(check_in)} → {_fmt_date(iso)}\n\n"
        "Your profile is saved — you won't need to do this again. "
        "I'm showing you the right services and prices for your villa now! 🛎️",
    )
    return True


# ── Public entry point ────────────────────────────────────────────────────────

async def handle_if_needed(
    sender_id: str, message_payload: dict, *, start_if_new: bool = False
) -> bool:
    """
    Called from two places in whatsapp_func.py:

    1. Session continuation (near top of process_message):
       handle_if_needed(sender_id, payload)   ← start_if_new defaults to False
       Only handles if sender already has an active mid-flow session.

    2. Order Services entry point:
       handle_if_needed(sender_id, payload, start_if_new=True)
       Starts a new onboarding flow if sender has no villa code AND no active session.

    Returns True  → this module handled the message; caller must return immediately.
    Returns False → sender is set up (or not mid-flow); caller continues normally.
    """
    # Always handle if mid-flow (regardless of start_if_new)
    session = _sessions.get(sender_id)
    if session:
        text = _extract_text(message_payload)
        if text is None:
            # Non-text during onboarding → re-prompt same step
            step = session.get("step")
            if step == "LOCATION":
                n = len(session.get("zones", []))
                await _send(sender_id, f"Please reply with a *number* between 1 and {n} to select your area.")
            elif step == "VILLA":
                n = len(session.get("villas", []))
                await _send(sender_id, f"Please reply with a *number* between 1 and {n} to select your villa.")
            elif step == "NAME":
                await _send(sender_id, "Please enter your *full name* — e.g. *Maria Santos*")
            elif step == "CHECKIN":
                await _send(sender_id, "Please enter your *check-in date* — e.g. *20 Jun 2026*")
            elif step == "CHECKOUT":
                await _send(sender_id, "Please enter your *check-out date* — e.g. *25 Jun 2026*")
            return True

        if session["step"] == "LOCATION":
            return await _handle_location_step(sender_id, session, text)
        if session["step"] == "VILLA":
            return await _handle_villa_step(sender_id, session, text)
        if session["step"] == "NAME":
            return await _handle_name_step(sender_id, session, text)
        if session["step"] == "CHECKIN":
            return await _handle_checkin_step(sender_id, session, text)
        if session["step"] == "CHECKOUT":
            return await _handle_checkout_step(sender_id, session, text)

    # No active session — only start a new one if explicitly requested
    if not start_if_new:
        return False

    # start_if_new=True (Order Services entry): check if setup is needed
    villa_code = await _get_villa_code(sender_id)
    if villa_code:
        return False  # Already has villa code — proceed to Order Services

    if await _is_service_provider(sender_id):
        return False  # SPs have their own flow

    return await _start_onboarding(sender_id)
