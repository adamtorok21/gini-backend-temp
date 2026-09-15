"""
customer_context.py - Authoritative customer villa/location resolver.

Design:
    The DB is the source of truth for villa_code and location_zone.
    Frontend payload values are treated as hints, not authoritative.

Resolution priority (highest -> lowest):
    1. villa-codes collection (set by QR scan -- most accurate, most recent)
    2. guest_profiles collection (set by registration form)
    3. Frontend payload (hint only -- used if DB has nothing)
    4. BLOCKED if none of the above yield a valid V-number

The returned CustomerContext is the single object that flows into order
creation. chatbot_routes.py must use this instead of request.villa_code
directly.
"""

import datetime
import re
from dataclasses import dataclass, field
from typing import Optional

from app.db.session import villa_code_collection, guest_profile_collection, villa_change_log_collection
from app.utils.logger import get_logger

logger = get_logger("customer_context")

_V_RE = re.compile(r"^V\d+$", re.IGNORECASE)


def _is_valid_v_code(code) -> bool:
    return bool(code and _V_RE.match(str(code).strip()))


# -- Result types --------------------------------------------------------------

@dataclass
class CustomerContext:
    """
    Resolved, authoritative customer context for a single booking request.

    Fields:
        villa_code      -- canonical V-number (e.g. "V1"), guaranteed valid
        location_zone   -- human-readable location (e.g. "Seminyak"), may be None
        source          -- where the villa_code came from
        customer_id     -- guest_id if found in guest_profiles
        phone_number    -- normalised phone number used for lookup
        resolved_at     -- UTC timestamp of resolution
        payload_villa   -- what the frontend sent (for mismatch logging)
        mismatch        -- True if payload_villa differs from DB-resolved villa_code
    """
    villa_code:    str
    location_zone: Optional[str]
    source:        str          # "qr_scan" | "guest_profile" | "payload_fallback"
    customer_id:   Optional[str] = None
    phone_number:  Optional[str] = None
    resolved_at:   datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    payload_villa: Optional[str] = None
    mismatch:      bool = False
    # Sub-source: actual user action that created the villa_code_collection record.
    # Set when source=="qr_scan". Values: "qr_scan" | "manual_entry" | "registration"
    scan_source:   Optional[str] = None
    # Timestamp from villa_code_collection.verified_at (when the record was last written)
    verified_at:   Optional[datetime.datetime] = None


@dataclass
class CustomerContextError:
    """Returned when no valid villa context can be resolved from any source."""
    reason:         str   # human-readable root cause
    code:           str   # machine-readable: MISSING_VILLA_CODE | NO_DB_RECORD
    sender_id:      Optional[str] = None
    phone_number:   Optional[str] = None
    payload_villa:  Optional[str] = None


# -- Normalisation helpers -----------------------------------------------------
# PHONE-NORM-01 (2026-08-26): this function's logic is now also available as
# app.utils.phone_utils.normalise_phone (byte-identical), extracted as the
# shared canonical version for new code (guest_routes.py, passport_routes.py
# now use it). Left duplicated here, unchanged, rather than refactored to
# import it — this is the documented, protected identity-resolution path;
# behavior-preserving in place is safer than a cross-module refactor of a
# hardened resolver. Do not let the two definitions drift apart.

def _normalise_phone(phone) -> str:
    """Canonical normalisation: strip spaces/dashes/parens/dots/+, strip leading 00. Digits only."""
    if not phone:
        return ""
    digits = re.sub(r"[\s\-\(\)\.\+]", "", str(phone))
    digits = re.sub(r"^00", "", digits)
    return digits


# -- Core resolver -------------------------------------------------------------

async def resolve_customer_context(
    sender_id: Optional[str] = None,
    phone_number: Optional[str] = None,
    payload_villa_code: Optional[str] = None,
    payload_location_zone: Optional[str] = None,
    fresh_context_confirmed: bool = False,
) -> "CustomerContext | CustomerContextError":
    """
    Resolve villa_code and location_zone for a customer from the DB.

    Args:
        sender_id           -- WhatsApp number or web session ID
        phone_number        -- phone number from booking form (may include country code)
        payload_villa_code  -- villa_code sent by the frontend (treated as hint)
        payload_location_zone -- location sent by the frontend (used as-is if DB has none)
        fresh_context_confirmed -- VILLA-FRESH-CONTEXT-01 (2026-08-28): True only
            when the guest just explicitly confirmed payload_villa_code THIS
            session (QR scan / manual entry, validated client-side against the
            live villa list) within a short window. When True and
            payload_villa_code is a valid V-number, it may override a Step-2
            guest_profile record — a guest who returns to a different villa on
            a later visit (e.g. V1 in June, V3 in August) must be trusted, not
            silently kept on their old villa. This NEVER overrides Step-1 QR
            scan (villa_code_collection) — that stays the highest-priority,
            physically-verified source, unchanged. Defaults False so every
            existing caller that doesn't pass it keeps the original DB-wins
            behavior for Step 2 exactly as before.

    Returns:
        CustomerContext on success, CustomerContextError if unresolvable.
    """
    resolved_villa: Optional[str] = None
    resolved_location: Optional[str] = None
    source: str = "payload_fallback"
    customer_id: Optional[str] = None
    used_phone: Optional[str] = None
    scan_source: Optional[str] = None    # sub-source from villa_code_collection.source field
    verified_at: Optional[datetime.datetime] = None  # from villa_code_collection.verified_at

    # -- Step 1: QR scan lookup (villa-codes collection, keyed by sender_id) --
    # This is the most authoritative source - set when the guest scans the villa QR
    if sender_id and sender_id.isdigit():
        try:
            vc_doc = await villa_code_collection.find_one({"sender_id": sender_id})
            if vc_doc:
                _vc = (vc_doc.get("villa_code") or "").strip()
                if _is_valid_v_code(_vc):
                    resolved_villa = _vc.upper()
                    source = "qr_scan"
                    scan_source = vc_doc.get("source")       # "qr_scan"|"manual_entry"|"registration"
                    verified_at = vc_doc.get("verified_at")  # datetime of last write
                    logger.info("CTX", f"QR scan resolved: sender={sender_id}, villa={resolved_villa}, scan_src={scan_source}")
        except Exception as e:
            logger.warning("CTX", f"villa_code_collection lookup failed: {e}")

    # -- Step 2: Guest profile lookup (by phone number) -----------------------
    # Registration form stores villa_code + location_zone
    if not resolved_villa:
        phones_to_try = []
        if phone_number:
            _norm = _normalise_phone(phone_number)
            phones_to_try.append(_norm)
            # Also try with leading 62 / 0 variants
            if _norm.startswith("62"):
                phones_to_try.append("0" + _norm[2:])
            elif _norm.startswith("0"):
                phones_to_try.append("62" + _norm[1:])
        if sender_id and sender_id.isdigit() and sender_id not in phones_to_try:
            phones_to_try.append(sender_id)

        stale_profile_villa: Optional[str] = None
        stale_profile_phone: Optional[str] = None
        for ph in phones_to_try:
            if not ph:
                continue
            try:
                gp = await guest_profile_collection.find_one({"phone_number": ph})
                if gp:
                    _vc = (gp.get("villa_code") or "").strip()
                    if _is_valid_v_code(_vc):
                        stale_profile_villa = _vc.upper()
                        stale_profile_phone = ph
                        resolved_villa = _vc.upper()
                        resolved_location = gp.get("location_zone") or None
                        customer_id = gp.get("guest_id")
                        used_phone = ph
                        source = "guest_profile"
                        logger.info(
                            "CTX",
                            f"Guest profile resolved: phone={ph}, "
                            f"villa={resolved_villa}, location={resolved_location}",
                        )
                        break
            except Exception as e:
                logger.warning("CTX", f"guest_profile_collection lookup failed for {ph}: {e}")

        # VILLA-FRESH-CONTEXT-01 (2026-08-28, live report — Clay): a guest
        # who explicitly confirmed a NEW villa this session (fresh_context_
        # confirmed=True, e.g. re-scanned/re-entered a villa code on a later
        # visit) was being silently kept on their OLD villa_code from a
        # stale guest_profile record — e.g. V1 in June, V3 in August, system
        # kept using V1. This is the correct scenario for a repeat guest
        # visiting a different villa, not a bug to suppress. Only fires when
        # a stale profile was actually found AND differs from the fresh
        # payload AND the payload is a validated V-number — never widens to
        # "any payload wins" (that would reopen CR-1's anti-spoofing
        # protection, still enforced for every ordinary, non-fresh request).
        if (
            fresh_context_confirmed
            and stale_profile_villa
            and payload_villa_code
            and _is_valid_v_code(payload_villa_code)
            and payload_villa_code.strip().upper() != stale_profile_villa
        ):
            _new_villa = payload_villa_code.strip().upper()
            logger.info(
                "CTX",
                f"VILLA_CHANGE: fresh_context_confirmed override — phone={stale_profile_phone}, "
                f"old_villa={stale_profile_villa}, new_villa={_new_villa}",
            )
            resolved_villa = _new_villa
            resolved_location = None  # re-resolved below from the new villa's sheet row
            source = "fresh_context_override"
            try:
                await guest_profile_collection.update_one(
                    {"phone_number": stale_profile_phone},
                    {"$set": {"villa_code": _new_villa, "villa_context_updated_at": datetime.datetime.utcnow()}},
                )
                await villa_change_log_collection.insert_one({
                    "phone_number": stale_profile_phone,
                    "old_villa_code": stale_profile_villa,
                    "new_villa_code": _new_villa,
                    "source": "fresh_context_override",
                    "timestamp": datetime.datetime.utcnow(),
                })
            except Exception as e:
                # Non-fatal: the override still applies to THIS request even
                # if persisting it fails — a guest must never be blocked or
                # silently reverted because a DB write hiccuped.
                logger.warning("CTX", f"Failed to persist villa change for {stale_profile_phone}: {e}")

    # -- Step 3: Payload fallback ---------------------------------------------
    # Only used if DB has nothing. Validated as V-number before trusting.
    if not resolved_villa:
        _pvc = (payload_villa_code or "").strip()
        if _is_valid_v_code(_pvc):
            resolved_villa = _pvc.upper()
            source = "payload_fallback"
            logger.warning(
                "CTX",
                f"PAYLOAD_FALLBACK: No DB record for sender={sender_id}, "
                f"phone={phone_number}. Using payload villa={resolved_villa}. "
                f"QR scan and registration are both absent.",
            )
        else:
            # Nothing usable anywhere
            logger.error(
                "CTX",
                f"RESOLUTION_FAILED: No valid villa_code from DB or payload. "
                f"sender={sender_id}, phone={phone_number}, payload_villa={payload_villa_code!r}",
            )
            return CustomerContextError(
                reason=(
                    f"No villa context found for this customer. "
                    f"DB lookup (QR scan + registration) found nothing. "
                    f"Payload villa_code={payload_villa_code!r} is not a valid V-number. "
                    f"Guest must scan the villa QR code or complete registration with a valid villa."
                ),
                code="MISSING_VILLA_CODE",
                sender_id=sender_id,
                phone_number=phone_number,
                payload_villa=payload_villa_code,
            )

    # -- Resolve location if still missing ------------------------------------
    if not resolved_location:
        # Try payload location if it's a real place name (not a V-code)
        _pl = (payload_location_zone or "").strip()
        if _pl and not _is_valid_v_code(_pl):
            resolved_location = _pl
        else:
            # Resolve from the Villas sheet via the V-number
            try:
                from app.services.menu_services import get_villa_location_by_code
                resolved_location = await get_villa_location_by_code(resolved_villa)
            except Exception as e:
                logger.warning("CTX", f"Villa location lookup failed for {resolved_villa}: {e}")

    # -- Mismatch detection ---------------------------------------------------
    mismatch = False
    _pvc_clean = (payload_villa_code or "").strip().upper()
    if _pvc_clean and _pvc_clean != resolved_villa and source != "payload_fallback":
        mismatch = True
        logger.warning(
            "CTX",
            f"MISMATCH: frontend sent villa={payload_villa_code!r} but "
            f"DB resolved villa={resolved_villa} (source={source}). "
            f"DB value will be used. sender={sender_id}, phone={phone_number}",
        )

    return CustomerContext(
        villa_code=resolved_villa,
        location_zone=resolved_location,
        source=source,
        customer_id=customer_id,
        phone_number=used_phone or phone_number,
        payload_villa=payload_villa_code,
        mismatch=mismatch,
        scan_source=scan_source,
        verified_at=verified_at,
    )
