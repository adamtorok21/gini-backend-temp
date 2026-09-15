"""
booking_links.py — Centralized booking URL generator.

Every booking link sent to a customer MUST be produced by this module.
Never construct /booking?... URLs manually elsewhere in the codebase.

Why this matters:
  villa_code is required for automated villa commission payout.
  If a booking link omits villa_code, the order is created without it,
  the Xendit disbursement skips the villa share, and commission is lost silently.
  This module makes it structurally impossible to emit a link without villa_code.
"""

from urllib.parse import quote
from app.settings.config import settings


def build_booking_url(
    *,
    villa_code: str,
    service: str,
    price: int,
    wa_number: str = "",
    location: str = "",
) -> str:
    """
    Build a /booking page URL that carries all payout-routing context.

    Args:
        villa_code:  V-number (e.g. "V1") — REQUIRED. Raises ValueError if absent.
        service:     Service item name, e.g. "Bali Massage 60min"
        price:       Integer price in IDR (per person)
        wa_number:   Customer WhatsApp number for polling (optional)
        location:    Human-readable location label (optional, for display)

    Returns:
        Full URL string, e.g.:
        https://www.ginibali.com/booking?service=Bali+Massage+60min&price=500000&wa=62812xxx&villa=V1&location=Seminyak

    Raises:
        ValueError: if villa_code is missing or not a V-number.
    """
    import re
    if not villa_code or not re.match(r"^V\d+$", str(villa_code).strip(), re.IGNORECASE):
        raise ValueError(
            f"build_booking_url: villa_code is required and must be a V-number (e.g. V1). "
            f"Got: {villa_code!r}. "
            f"Booking links without villa_code will break automated villa commission payouts."
        )

    base = getattr(settings, "WEB_BASE_URL", "https://www.ginibali.com")
    params = f"service={quote(str(service))}&price={int(price)}&villa={quote(str(villa_code))}"
    if wa_number:
        params += f"&wa={quote(str(wa_number))}"
    if location:
        params += f"&location={quote(str(location))}"

    return f"{base}/booking?{params}"


def build_whatsapp_booking_message(
    *,
    villa_code: str,
    service: str,
    price: int,
    wa_number: str = "",
    location: str = "",
) -> str:
    """
    Build the full WhatsApp share message that includes the booking URL.
    The booking URL always embeds villa_code.
    """
    url = build_booking_url(
        villa_code=villa_code,
        service=service,
        price=price,
        wa_number=wa_number,
        location=location,
    )
    return (
        f"📋 *Booking Link*\n\n"
        f"Service: {service}\n"
        f"Price: IDR {price:,}\n\n"
        f"Tap to book: {url}"
    )
