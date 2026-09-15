"""
phone_utils.py — canonical phone normalization, shared across every write
and lookup path that keys a guest by phone number.

PHONE-NORM-01 (2026-08-26): previously three independent, near-identical
copies of `_normalise_phone()` existed (guest_routes.py, passport_routes.py,
customer_context.py), and only customer_context.py's resolver additionally
tried the `62`/`0` prefix-variant of a number before giving up. That variant
expansion is correct, standard Indonesian phone-format handling — `62812...`
(international) and `0812...` (national) are the SAME number, not two
different guests' numbers colliding — but because it lived only inside the
read-path resolver, the two WRITE paths that key `guest_profile_collection`
by phone (`POST /guest/register`, `PATCH /passports/{id}/link-guest`) could
fail to find a guest's own existing profile if they had previously registered
under the other prefix form, creating a second, duplicate profile for the
same person — a direct violation of `link-guest`'s own documented
idempotency claim ("repeated calls with the same phone do not create
duplicate profiles").
"""

import re
from typing import Optional


def normalise_phone(raw) -> str:
    """Canonical normalisation: strip spaces/dashes/parens/dots/+, strip
    leading 00. Digits only. Never raises — returns "" for falsy input."""
    if not raw:
        return ""
    digits = re.sub(r"[\s\-\(\)\.\+]", "", str(raw))
    digits = re.sub(r"^00", "", digits)
    return digits


def phone_variants(raw) -> list[str]:
    """Return the normalised number plus its `62`/`0` prefix-variant, in
    that order, deduplicated. Both variants represent the SAME Indonesian
    phone number in international vs. national format — this is not a
    collision risk, it's correct format handling, matching
    customer_context.py's already-proven resolver logic."""
    norm = normalise_phone(raw)
    if not norm:
        return []
    variants = [norm]
    if norm.startswith("62"):
        alt = "0" + norm[2:]
    elif norm.startswith("0"):
        alt = "62" + norm[1:]
    else:
        alt = None
    if alt and alt not in variants:
        variants.append(alt)
    return variants


async def find_guest_profile_by_phone(collection, raw_phone) -> Optional[dict]:
    """Look up a guest_profile_collection document by phone, trying both the
    `62`/`0` prefix-variant forms before giving up. Returns None if no
    variant matches. Never raises internally beyond what `collection.find_one`
    itself would raise — callers already handle DB errors at their own level."""
    for variant in phone_variants(raw_phone):
        doc = await collection.find_one({"phone_number": variant})
        if doc:
            return doc
    return None
