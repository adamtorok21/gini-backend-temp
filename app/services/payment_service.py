import os
import re
import httpx
import xendit
import base64
from xendit.apis import InvoiceApi
from xendit.invoice.model.create_invoice_request import CreateInvoiceRequest
from xendit.invoice.model.customer_object import CustomerObject
from xendit.invoice.model.notification_preference import NotificationPreference
from xendit.invoice.model.notification_channel import NotificationChannel
from xendit.invoice.model.invoice_item import InvoiceItem
import datetime
from app.db.session import order_collection
from app.models.order_summary import Order, PayoutStatus
from app.settings.config import settings
from app.services.menu_services import get_villa_location_by_code
from app.services.promo_service import validate_promo_code
import logging
import asyncio


logger = logging.getLogger(__name__)

# Set API key
xendit.set_api_key(settings.XENDIT_SECRET_KEY)

# ── Disbursement threshold ──────────────────────────────────────────────────
# PROVEN: Xendit accepted IDR 4,638 (orders EB40, EB43 — Dec 2025).
# The 10,000 IDR floor introduced in Apr 2026 was a false assumption.
# Default is now 0 (no local pre-block) — Xendit is the sole gatekeeper.
# Set XENDIT_MIN_DISBURSEMENT_IDR to a positive integer only if Xendit
# explicitly returns an error proving they enforce a minimum.
XENDIT_MIN_DISBURSEMENT_IDR: int = int(os.getenv("XENDIT_MIN_DISBURSEMENT_IDR", "0"))

# ── Villa-specific minimum (independent of SP minimum) ─────────────────────
# Proven default: 0 (no local pre-block for villa — Xendit decides).
# Set XENDIT_VILLA_MIN_IDR to a non-zero value only with proven API evidence.
XENDIT_VILLA_MIN_IDR: int = int(os.getenv("XENDIT_VILLA_MIN_IDR", "0"))

# ── Villa test-mode override ────────────────────────────────────────────────
# TEST-ONLY: forces a specific villa commission amount into disbursement,
# bypassing the sheet value.  Use this to verify the full villa payout path
# (bank lookup → Xendit call → notification) without changing the sheet.
#
# Both conditions must be true to activate:
#   VILLA_DISBURSEMENT_TEST_AMOUNT > 0   (the override amount, e.g. 15000)
#   XENDIT_ENV=test                       (safety guard — never fires in prod)
#
# Set VILLA_DISBURSEMENT_TEST_AMOUNT=0 (default) to disable.
VILLA_DISBURSEMENT_TEST_AMOUNT: int = int(os.getenv("VILLA_DISBURSEMENT_TEST_AMOUNT", "0"))
XENDIT_ENV: str = os.getenv("XENDIT_ENV", "production")

# ── Sheet staleness threshold ───────────────────────────────────────────────
# Warn if Google Sheets cache is older than this many seconds (default: 4 h).
# Set SHEET_STALE_WARN_SECONDS=0 to disable the warning entirely.
SHEET_STALE_WARN_SECONDS: int = int(os.getenv("SHEET_STALE_WARN_SECONDS", "14400"))


def clean_price_string(price_str: str) -> int:
    cleaned = re.sub(r'[^\d]', '', str(price_str))
    if not cleaned:
        raise ValueError(f"No digits found in price string: '{price_str}'")
    return int(cleaned)


def build_payout_description(order_code: str, payout_type: str) -> str:
    """
    Standardised payout description, hard-capped at 14 characters.
    Ensures Suffix (SP, VL, EB) always survives; trims Order ID if needed.
    """
    mapping = {
        "service_provider": "SP",
        "villa": "VL",
        "eb": "EB",
    }
    suffix = mapping.get(payout_type, "PY")
    # Max length for order_code is 14 minus (hyphen + suffix length)
    max_order_len = 14 - (len(suffix) + 1)
    return f"{order_code[:max_order_len]}-{suffix}"


def _check_sheet_freshness() -> dict:
    """Return cache age info. Logs a warning when staleness exceeds threshold."""
    try:
        from app.services.menu_services import cache
        last_updated = cache.get("last_updated")
        if not last_updated:
            logger.warning("[Sheet] Cache last_updated is not set — sheet may never have loaded.")
            return {"fresh": False, "last_updated": None, "age_seconds": None, "warning": "never_loaded"}

        if isinstance(last_updated, str):
            last_updated = datetime.datetime.fromisoformat(last_updated)

        age_seconds = (datetime.datetime.now() - last_updated).total_seconds()
        is_stale = SHEET_STALE_WARN_SECONDS > 0 and age_seconds > SHEET_STALE_WARN_SECONDS

        if is_stale:
            logger.warning(
                f"[Sheet] Cache is {int(age_seconds)}s old (threshold: {SHEET_STALE_WARN_SECONDS}s). "
                f"Split amounts and prices may be outdated. Call POST /menu/refresh to reload."
            )

        return {
            "fresh": not is_stale,
            "last_updated": last_updated.isoformat(),
            "age_seconds": int(age_seconds),
            "warning": "stale" if is_stale else None,
        }
    except Exception as e:
        logger.warning(f"[Sheet] Could not check freshness: {e}")
        return {"fresh": True, "last_updated": None, "age_seconds": None, "warning": None}


def _disbursement_eligibility(amount: int, bank_details: dict, party: str) -> dict:
    """
    Returns a structured pre-check for a single disbursement party.
    Used to log and store what will happen before Xendit is called.

    Returns:
        {
          "party": "sp" | "villa",
          "intended_amount": int,
          "eligible": bool,
          "skip_reason": str | None,
        }
    """
    if not bank_details.get("bank_code") or not bank_details.get("account_number"):
        return {
            "party": party,
            "intended_amount": amount,
            "eligible": False,
            "skip_reason": "missing_bank_details",
        }
    effective_min = XENDIT_VILLA_MIN_IDR if party == "villa" else XENDIT_MIN_DISBURSEMENT_IDR
    if effective_min > 0 and amount < effective_min:
        return {
            "party": party,
            "intended_amount": amount,
            "eligible": False,
            "skip_reason": f"amount_below_minimum (IDR {amount:,} < IDR {effective_min:,})",
        }
    return {
        "party": party,
        "intended_amount": amount,
        "eligible": True,
        "skip_reason": None,
    }


async def get_service_provider_bank_details(provider_code: str) -> dict:
    """Fetch service provider bank details from API"""
    try:
        params = {"provider_code": provider_code}
        url = f"{settings.BASE_URL}/menu/service-provider-bank"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Error fetching SP bank details for '{provider_code}': {e}")
        return None


async def get_villa_bank_details(provider_code: str) -> dict:
    """Fetch villa bank details from API"""
    try:
        params = {"provider_code": provider_code}
        url = f"{settings.BASE_URL}/menu/villa-bank"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Error fetching villa bank details for '{provider_code}': {e}")
        return None


async def get_price_distribution(service_item: str, location_zone: str = None) -> dict:
    """Fetch price distribution for service item"""
    try:
        params = {"service_item": service_item}
        if location_zone:
            params["location_zone"] = location_zone
        url = f"{settings.BASE_URL}/menu/price_distribution"
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        logger.error(f"Error fetching price distribution for '{service_item}': {e}")
        return None


async def create_xendit_payment_with_distribution(order: Order):
    """
    Create a Xendit invoice and lock split distribution data into the order.

    Source of truth hierarchy:
      1. Google Sheet (Prices Set tab) — sp_price, villa_price, total
      2. Services Overview "Final Price" column — total only
      3. Default 70/20/10 percentage split — if sheet unavailable

    Sheet freshness is checked and logged before every invoice creation.
    """
    from app.routes.main_menu_routes import (
        get_bank_details_for_provider,
        get_bank_details_for_villa,
        get_price_distribution_details,
    )

    try:
        # ── Sheet freshness check ────────────────────────────────────────────
        freshness = _check_sheet_freshness()
        logger.info(
            f"[Payment] Sheet freshness for order {order.order_number}: "
            f"age={freshness.get('age_seconds')}s, fresh={freshness.get('fresh')}, "
            f"last_updated={freshness.get('last_updated')}"
        )

        # Resolve location zone from villa_code for dynamic pricing
        location_zone = await get_villa_location_by_code(order.villa_code)

        # Get price distribution — DIRECT CALL instead of HTTP round-trip
        price_distribution = {}
        price_source = "default_split"
        try:
            price_distribution = await get_price_distribution_details(order.service_name, location_zone)
            if price_distribution:
                price_source = "google_sheet"
        except Exception as e:
            logger.warning(f"[Payment] Price distribution fetch failed for '{order.service_name}': {e}")

        # Get bank details — DIRECT CALL
        service_provider_bank = {}
        try:
            service_provider_bank = await get_bank_details_for_provider(order.service_provider_code)
        except Exception as e:
            logger.warning(f"[Payment] Bank details missing for SP '{order.service_provider_code}': {e}")
            service_provider_bank = {
                "provider_code": order.service_provider_code,
                "bank_code": "",
                "account_number": "",
            }

        villa_bank = {}
        try:
            villa_bank = await get_bank_details_for_villa(order.villa_code)
        except Exception as e:
            logger.warning(f"[Payment] Bank details missing for Villa '{order.villa_code}': {e}")
            villa_bank = {
                "provider_code": order.villa_code,
                "bank_code": "",
                "account_number": "",
            }

        external_id = f"booking_{order.order_number}_{int(datetime.datetime.now().timestamp())}"

        try:
            total_price_clean = clean_price_string(order.price)

            # Apply promo discount if present
            final_price = total_price_clean
            if hasattr(order, "promo_code") and order.promo_code:
                is_valid, discounted_price, message = await validate_promo_code(
                    order.promo_code, total_price_clean
                )
                if is_valid:
                    final_price = int(discounted_price)
                    logger.info(f"[Payment] Promo '{order.promo_code}' applied: {message}")
                else:
                    logger.warning(f"[Payment] Invalid promo '{order.promo_code}': {message}")

            # ── Calculate split amounts ──────────────────────────────────────
            if (
                price_distribution
                and price_distribution.get("service_provider_price")
                and price_distribution.get("villa_price")
            ):
                try:
                    sp_price_orig = clean_price_string(str(price_distribution["service_provider_price"]))
                    villa_price_orig = clean_price_string(str(price_distribution["villa_price"]))

                    if final_price < total_price_clean and total_price_clean > 0:
                        ratio = final_price / total_price_clean
                        service_provider_price = int(sp_price_orig * ratio)
                        villa_price = int(villa_price_orig * ratio)
                    else:
                        service_provider_price = sp_price_orig
                        villa_price = villa_price_orig

                    easy_bali_price = final_price - (service_provider_price + villa_price)
                    price_source = "google_sheet"
                except (ValueError, KeyError) as e:
                    logger.warning(
                        f"[Payment] Sheet price parsing failed ({e}) — falling back to default 70/20/10 split"
                    )
                    service_provider_price = int(final_price * 0.70)
                    villa_price = int(final_price * 0.20)
                    easy_bali_price = final_price - service_provider_price - villa_price
                    price_source = "default_split"
            else:
                logger.warning(
                    f"[Payment] No sheet price distribution for '{order.service_name}' "
                    f"— applying default 70/20/10 split (sheet may be stale or service missing)"
                )
                service_provider_price = int(final_price * 0.70)
                villa_price = int(final_price * 0.20)
                easy_bali_price = final_price - service_provider_price - villa_price
                price_source = "default_split"

            # ── Pre-disbursement eligibility check ───────────────────────────
            # Computed now and stored in distribution_data so the webhook can
            # see intent vs actual outcome without re-computing.
            sp_eligibility = _disbursement_eligibility(
                service_provider_price, service_provider_bank, "sp"
            )
            villa_eligibility = _disbursement_eligibility(
                villa_price, villa_bank, "villa"
            )
            logger.info(
                f"[Payment] Split plan for {order.order_number}: "
                f"total={final_price:,} IDR | "
                f"SP={service_provider_price:,} (eligible={sp_eligibility['eligible']}, skip={sp_eligibility['skip_reason']}) | "
                f"Villa={villa_price:,} (eligible={villa_eligibility['eligible']}, skip={villa_eligibility['skip_reason']}) | "
                f"EB={easy_bali_price:,} | source={price_source}"
            )

        except ValueError as e:
            logger.error(f"[Payment] Price cleaning error: {e}")
            return {"success": False, "error": f"Invalid price format: {order.price}"}

        # ── Create Xendit invoice ─────────────────────────────────────────────
        config = xendit.Configuration(api_key=settings.XENDIT_SECRET_KEY)
        api_client = xendit.ApiClient(configuration=config)
        api_instance = InvoiceApi(api_client)

        try:
            date_str = order.date.strftime("%d-%m-%Y") if order.date else "TBD"
        except AttributeError:
            date_str = str(order.date) if order.date else "TBD"

        # Channel-aware post-payment landing (Clay/Adam, 2026-08-14):
        #   WhatsApp order (sender_id is a phone number) → /order-confirmed page,
        #     which shows the receipt + a "Back to WhatsApp" button (a browser tab
        #     opened by a redirect cannot be force-closed, so this is the best UX).
        #   Website order (sender_id is a web id like 'web' / 'user_...') → back to
        #     /chatbot with the prior conversation restored and the receipt shown.
        _is_whatsapp = str(order.sender_id or "").isdigit()
        if _is_whatsapp:
            _success_url = f"{settings.WEB_BASE_URL}/order-confirmed?order={order.order_number}"
        else:
            # ORDER-PAYMENT-CROSS-TAB-UID-01 (2026-09-01): the payment link
            # opens in a NEW browser tab (target="_blank" in chat.jsx) --
            # Xendit's success redirect therefore lands in that new tab, not
            # the one that placed the order. If that new tab is a genuinely
            # separate browsing context (e.g. a fresh incognito window used
            # to test the link), chatApi.getUserId() mints a brand-new
            # random userId there, and the order-services-nav chat history
            # (keyed by the ORIGINAL tab's userId) becomes unreachable --
            # the guest lands on a correct-looking but history-less Order
            # Services tab. Carrying the order's own sender_id (== the
            # userId that placed it) as a URL param lets the new tab's
            # _paidOrder restore branch prefer it over minting a fresh one.
            _success_url = (
                f"{settings.WEB_BASE_URL}/chatbot?order={order.order_number}"
                f"&paid=1&uid={order.sender_id or ''}"
            )

        create_invoice_request = CreateInvoiceRequest(
            external_id=external_id,
            amount=float(final_price),
            currency="IDR",
            invoice_duration=float(settings.XENDIT_INVOICE_DURATION_SECONDS),
            description=f"Payment for {order.service_name} - {date_str}",
            customer=CustomerObject(
                given_names="Customer",
                mobile_number=order.sender_id,
            ),
            customer_notification_preference=NotificationPreference(
                invoice_created=[NotificationChannel("whatsapp")],
                invoice_reminder=[NotificationChannel("whatsapp")],
                invoice_paid=[NotificationChannel("whatsapp")],
            ),
            success_redirect_url=_success_url,
            failure_redirect_url=f"{settings.WEB_BASE_URL}/payment-failed?order={order.order_number}",
            webhook_url=f"{settings.XENDIT_WEBHOOK_BASE_URL}/webhook/xendit-payment",
            payment_methods=[
                "CREDIT_CARD", "BCA", "BNI", "BSI", "BRI", "MANDIRI", "PERMATA",
                "SAHABAT_SAMPOERNA", "BNC", "ALFAMART", "INDOMARET", "OVO", "DANA",
                "SHOPEEPAY", "LINKAJA", "JENIUSPAY", "DD_BRI", "DD_BCA_KLIKPAY", "QRIS",
            ],
            items=[
                InvoiceItem(
                    name=order.service_name,
                    quantity=1.0,
                    price=float(final_price),
                )
            ],
        )

        api_response = api_instance.create_invoice(create_invoice_request)

        distribution_data = {
            "service_provider": {
                "amount": service_provider_price,
                "bank_details": service_provider_bank,
                "eligibility": sp_eligibility,
            },
            "villa": {
                "amount": villa_price,
                "bank_details": villa_bank,
                "eligibility": villa_eligibility,
            },
            "easy_bali": {
                "amount": easy_bali_price,
                "description": "Platform commission",
            },
            "total_distribution": service_provider_price + villa_price + easy_bali_price,
            "price_source": price_source,
            "sheet_freshness": freshness,
            "threshold_idr": XENDIT_MIN_DISBURSEMENT_IDR,
        }

        return {
            "success": True,
            "invoice_id": api_response.id,
            "payment_url": api_response.invoice_url,
            "external_id": external_id,
            "expires_at": api_response.expiry_date,
            "distribution_data": distribution_data,
        }

    except xendit.XenditSdkException as e:
        logger.error(f"[Payment] Xendit SDK error for order {order.order_number}: {e}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"[Payment] Invoice creation error for order {order.order_number}: {e}")
        return {"success": False, "error": str(e)}


async def update_order_with_payment_info(order_number: str, payment_data: dict):
    """Update order with payment and distribution information"""
    try:
        update_data = {
            "payment.xendit_invoice_id": payment_data.get("invoice_id"),
            "payment.payment_url": payment_data.get("payment_url"),
            "payment.external_id": payment_data.get("external_id"),
            "payment.payment_status": "pending",
            "payment.distribution_data": payment_data.get("distribution_data"),
            "payment.link_sent_at": datetime.datetime.now(),
            "status": "payment_pending",
            "updated_at": datetime.datetime.now(),
        }
        result = await order_collection.update_one(
            {"order_number": order_number}, {"$set": update_data}
        )

        # Transition fsm_payment_status UNPAID → PAYMENT_LINK_SENT so the EXPIRED
        # webhook can later use the valid PAYMENT_LINK_SENT → EXPIRED path.
        try:
            from app.services.status_service import BookingStatusManager
            from app.models.order_summary import PaymentStatus
            await BookingStatusManager.transition_payment_status(
                order_number,
                PaymentStatus.PAYMENT_LINK_SENT,
                "PAYMENT_SERVICE",
                reason="Payment link generated and sent to guest",
            )
        except Exception as _fsm_err:
            logger.warning(f"[Payment] FSM PAYMENT_LINK_SENT transition failed for {order_number}: {_fsm_err}")

        return result.modified_count > 0
    except Exception as e:
        logger.error(f"[Payment] DB update error for {order_number}: {e}")
        return False


async def get_xendit_cash_balance(client: httpx.AsyncClient) -> int:
    """
    Query Xendit CASH (disbursement) balance.
    Returns the integer balance in IDR, or None on error.
    """
    try:
        token = base64.b64encode(f"{settings.XENDIT_SECRET_KEY}:".encode()).decode()
        r = await client.get(
            "https://api.xendit.co/balance?account_type=CASH",
            headers={"Authorization": f"Basic {token}"},
            timeout=httpx.Timeout(10.0),
        )
        r.raise_for_status()
        balance = r.json().get("balance", 0)
        logger.info(f"[Balance] Xendit CASH balance: IDR {balance:,}")
        return int(balance)
    except Exception as e:
        logger.warning(f"[Balance] Could not fetch Xendit balance: {e}")
        return None


async def create_bank_disbursement(
    client: httpx.AsyncClient,
    amount: int,
    bank_details: dict,
    reference_id: str,
    description: str,
    min_amount: int = None,
) -> dict:
    """
    Attempt a single Xendit bank disbursement with retry logic.

    min_amount: local pre-flight guard. Pass 0 to let Xendit be the sole
    gatekeeper (so we see the real API response rather than skipping locally).
    Defaults to XENDIT_MIN_DISBURSEMENT_IDR when not specified.

    Returns a structured result dict:
      {"success": True,  "disbursement_id": ..., "status": ..., "amount": ...}
      {"success": False, "skipped": True,  "reason": ..., "amount": ...}
      {"success": False, "error": ...,     "amount": ...}
    """
    if min_amount is None:
        min_amount = XENDIT_MIN_DISBURSEMENT_IDR

    # ── Pre-flight minimum guard ─────────────────────────────────────────────
    # PROVEN: Xendit accepted IDR 4,638 (EB40, EB43 — Dec 2025).
    # Default min_amount=0 (disabled). Only activate if Xendit explicitly
    # rejects an amount with a proven API error response.
    if min_amount > 0 and amount < min_amount:
        logger.warning(
            f"[Disbursement] Skipping {reference_id}: IDR {amount:,} is below "
            f"local guard IDR {min_amount:,}. "
            f"Set XENDIT_VILLA_MIN_IDR=0 (or XENDIT_MIN_DISBURSEMENT_IDR=0) "
            f"to bypass this guard and let Xendit decide."
        )
        return {
            "success": False,
            "skipped": True,
            "reason": f"amount_below_minimum: IDR {amount:,} < IDR {min_amount:,}",
            "amount": amount,
        }

    # ── Pre-flight balance check ─────────────────────────────────────────────
    # Check Xendit CASH balance before attempting. If balance is insufficient,
    # fail immediately with a clear reason instead of submitting to Xendit and
    # letting it silently FAIL later with INSUFFICIENT_BALANCE.
    # This was the root cause of EB69/EB680/EB722 failing — balance was -1,059 IDR.
    cash_balance = await get_xendit_cash_balance(client)
    if cash_balance is not None and cash_balance < amount:
        logger.error(
            f"[Disbursement] INSUFFICIENT_BALANCE for {reference_id}: "
            f"need IDR {amount:,}, have IDR {cash_balance:,}. "
            f"Top up Xendit CASH balance at dashboard.xendit.co before retrying."
        )
        return {
            "success": False,
            "skipped": False,
            "reason": f"INSUFFICIENT_BALANCE: need IDR {amount:,}, have IDR {cash_balance:,}",
            "amount": amount,
            "xendit_cash_balance": cash_balance,
        }

    disbursement_payload = {
        "external_id": reference_id,
        "amount": amount,
        "bank_code": str(bank_details.get("bank_code", "")),
        "account_holder_name": str(
            bank_details.get("account_holder_name") or bank_details.get("account_name", "")
        ),
        "account_number": bank_details.get("account_number", ""),
        "description": description,
    }

    token = base64.b64encode(f"{settings.XENDIT_SECRET_KEY}:".encode()).decode()
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "X-IDEMPOTENCY-KEY": reference_id,
    }

    # Always log exact payload so we have evidence regardless of outcome
    logger.info(
        f"[Disbursement] Sending to Xendit — reference_id={reference_id} "
        f"payload={disbursement_payload}"
    )

    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            response = await client.post(
                "https://api.xendit.co/disbursements",
                json=disbursement_payload,
                headers=headers,
                timeout=httpx.Timeout(15.0),
            )
            # Always log status + raw body before raise_for_status so we
            # capture any Xendit error detail (especially on 4xx rejections).
            logger.info(
                f"[Disbursement] Xendit response — reference_id={reference_id} "
                f"attempt={attempt + 1} status={response.status_code} body={response.text[:500]}"
            )
            response.raise_for_status()
            result = response.json()
            logger.info(
                f"[Disbursement] {reference_id} succeeded on attempt {attempt + 1}: "
                f"id={result.get('id')}, status={result.get('status')}, amount={result.get('amount')}"
            )
            return {
                "success": True,
                "disbursement_id": result.get("id"),
                "status": result.get("status"),
                "amount": result.get("amount"),
                "xendit_response": result,
            }
        except httpx.TimeoutException as te:
            last_error = f"timeout: {te}"
            logger.warning(
                f"[Disbursement] Timeout on attempt {attempt + 1}/{max_retries} for {reference_id}"
            )
        except httpx.HTTPStatusError as e:
            last_error = f"HTTP {e.response.status_code}: {e.response.text}"
            logger.error(
                f"[Disbursement] HTTP error on attempt {attempt + 1}/{max_retries} for {reference_id}: {last_error}"
            )
            # 4xx errors are not retryable (bad request, auth, etc.)
            if e.response.status_code < 500:
                return {
                    "success": False,
                    "error": last_error,
                    "amount": amount,
                    "xendit_status_code": e.response.status_code,
                    "xendit_error_body": e.response.text,
                }
        except httpx.HTTPError as e:
            last_error = str(e)
            logger.error(
                f"[Disbursement] Network error on attempt {attempt + 1}/{max_retries} for {reference_id}: {e}"
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)

    return {"success": False, "error": f"Failed after {max_retries} retries: {last_error}", "amount": amount}


async def distribute_order_payments(
    order_number: str,
    distribution_data: dict,
    paid_amount: int = None,
) -> dict:
    """
    Execute bank disbursements for SP and Villa and persist all payout state to DB.

    Design contract:
    - PayoutContext is built and validated BEFORE any Xendit call is made.
    - If context is invalid (missing SP code, missing villa code, reconciliation
      failure) → blocked state is written to DB and function returns immediately.
    - SP and Villa are treated symmetrically: both have explicit payout states.
    - No silent skips. Every outcome is stored in the DB document.
    - Bank details are always re-fetched from the live sheet at disbursement time.

    Returns a structured summary used by xendit_webhook.py for notifications:
    {
      "service_provider": {"success": bool, ...},
      "villa":            {"success": bool, ...},
      "distributed_at":  datetime,
    }
    """
    from app.models.order_summary import SpPayoutStatus
    from app.models.payout_context import PayoutContext, PayoutContextError
    from app.routes.main_menu_routes import (
        get_bank_details_for_provider as _get_sp_bank,
        get_bank_details_for_villa as _get_villa_bank,
    )

    disbursements: dict = {}

    # ── 1. Fetch live order document ─────────────────────────────────────────
    _order_doc = await order_collection.find_one({"order_number": order_number})
    if not _order_doc:
        logger.error(f"[Disbursement] Order {order_number} not found in DB — cannot disburse")
        return {"service_provider": {"success": False, "error": "order_not_found"}, "villa": {"success": False, "error": "order_not_found"}}

    # Merge live doc's distribution_data (preferred) with caller-supplied fallback
    _live_dist = (_order_doc.get("payment") or {}).get("distribution_data")
    _dist = _live_dist or distribution_data

    # Inject the merged dist into a temporary doc view for PayoutContext.build()
    _doc_for_context = dict(_order_doc)
    if "payment" not in _doc_for_context:
        _doc_for_context["payment"] = {}
    _doc_for_context["payment"] = {**(_doc_for_context.get("payment") or {}), "distribution_data": _dist}

    # ── CRITICAL INVARIANT ────────────────────────────────────────────────────
    # PP-1, PP-2: PayoutContext.build() MUST be called before any Xendit call.
    # If it returns PayoutContextError, ALL disbursement is blocked — no Xendit
    # calls may be made, and the BLOCKED status must be persisted to the order.
    # Do NOT move or remove this call. Do NOT add Xendit calls above this line.
    # Enforced by: tests/protected_flow/test_payout_context_gate.py
    # ─────────────────────────────────────────────────────────────────────────
    # ── 2. Build and validate PayoutContext ──────────────────────────────────
    ctx_or_err = PayoutContext.build(_doc_for_context, paid_amount=paid_amount)

    if isinstance(ctx_or_err, PayoutContextError):
        err = ctx_or_err
        logger.warning(
            f"[Disbursement] PAYOUT_CONTEXT_INVALID for {order_number}: "
            f"code={err.code} message={err.message}"
        )

        # Map error code → specific payout status values
        _villa_status = {
            "MISSING_VILLA_CODE":        PayoutStatus.BLOCKED_MISSING_VILLA_CODE,
            "MISSING_SP_CODE":           PayoutStatus.PENDING,       # villa not the root cause
            "MISSING_DISTRIBUTION_DATA": PayoutStatus.BLOCKED_DISTRIBUTION_DATA,
            "RECONCILIATION_FAILURE":    PayoutStatus.BLOCKED_RECONCILIATION,
            "PAID_AMOUNT_MISMATCH":      PayoutStatus.BLOCKED_RECONCILIATION,
            "MISSING_LOCATION_ZONE":     PayoutStatus.PENDING,
        }.get(err.code, PayoutStatus.FAILED)

        _sp_status = {
            "MISSING_SP_CODE":           SpPayoutStatus.BLOCKED_MISSING_CODE,
            "MISSING_VILLA_CODE":        SpPayoutStatus.PENDING,     # SP not the root cause
            "MISSING_DISTRIBUTION_DATA": SpPayoutStatus.BLOCKED_MISSING_CODE,
            "RECONCILIATION_FAILURE":    SpPayoutStatus.PENDING,
            "PAID_AMOUNT_MISMATCH":      SpPayoutStatus.PENDING,
            "MISSING_LOCATION_ZONE":     SpPayoutStatus.PENDING,
        }.get(err.code, SpPayoutStatus.PENDING)

        try:
            await order_collection.update_one(
                {"order_number": order_number},
                {"$set": {
                    "payout_status":             _villa_status,
                    "payout_blocked_reason":     err.message,
                    "sp_payout_status":          _sp_status,
                    "sp_payout_blocked_reason":  err.message if _sp_status in SpPayoutStatus.ALL_BLOCKED else None,
                    "payout_reconciliation_error": err.message if err.code in ("RECONCILIATION_FAILURE", "PAID_AMOUNT_MISMATCH") else None,
                    "payment.retryable":         True,
                }},
            )
        except Exception as _dbe:
            logger.error(f"[Disbursement] Failed to persist context-invalid state for {order_number}: {_dbe}")

        await _send_payout_admin_alert(
            order_number=order_number,
            sp_ok=False, villa_ok=False,
            sp_reason=err.message, vl_reason=err.message,
            cash_bal=None,
            extra_note=f"Context validation failed: [{err.code}]",
        )
        return {
            "service_provider": {"success": False, "error": err.code, "message": err.message},
            "villa":            {"success": False, "error": err.code, "message": err.message},
            "distributed_at":   datetime.datetime.now(),
        }

    ctx: PayoutContext = ctx_or_err
    logger.info(f"[Disbursement] PayoutContext validated: {ctx.summary()}")

    # ── CRITICAL INVARIANT ────────────────────────────────────────────────────
    # PP-4, PP-5: Bank details MUST be re-fetched live from Google Sheets here.
    # Do NOT use ctx.sp_bank or ctx.villa_bank directly for Xendit without first
    # calling _get_sp_bank() and _get_villa_bank(). Invoice-creation snapshots
    # may be stale or empty. The live sheet is always authoritative.
    # Enforced by: tests/protected_flow/test_live_bank_fetch.py
    # ─────────────────────────────────────────────────────────────────────────
    # ── 3. Re-fetch bank details from live sheet ─────────────────────────────
    # Invoice-creation bank details may be stale (TBD, empty account). Always re-fetch.
    sp_bank = ctx.sp_bank
    try:
        fresh_sp = await _get_sp_bank(ctx.service_provider_code)
        if fresh_sp.get("bank_code") and fresh_sp.get("account_number"):
            sp_bank = fresh_sp
            logger.info(f"[Disbursement] SP bank refreshed: code={ctx.service_provider_code} bank={fresh_sp.get('bank_code')}")
        else:
            logger.warning(f"[Disbursement] SP bank sheet lookup empty for {ctx.service_provider_code} — using stored data")
    except Exception as _e:
        logger.warning(f"[Disbursement] SP bank re-fetch failed for {ctx.service_provider_code}: {_e} — using stored data")

    villa_bank = ctx.villa_bank
    try:
        fresh_villa = await _get_villa_bank(ctx.villa_code)
        if fresh_villa.get("bank_code") and fresh_villa.get("account_number"):
            villa_bank = fresh_villa
            logger.info(f"[Disbursement] Villa bank refreshed: code={ctx.villa_code} bank={fresh_villa.get('bank_code')}")
        else:
            logger.warning(f"[Disbursement] Villa bank sheet lookup empty for {ctx.villa_code} — using stored data")
    except Exception as _e:
        logger.warning(f"[Disbursement] Villa bank re-fetch failed for {ctx.villa_code}: {_e} — using stored data")

    # ── 4. Pre-disbursement bank validation ──────────────────────────────────
    # Both SP and Villa bank readiness is checked BEFORE calling Xendit.
    # Missing bank → explicit blocked state → no Xendit call.
    sp_bank_ready = bool(sp_bank.get("bank_code") and sp_bank.get("account_number"))
    villa_bank_ready = bool(villa_bank.get("bank_code") and villa_bank.get("account_number"))

    if not sp_bank_ready:
        _blocked = {
            "order_id": order_number, "sp_code": ctx.service_provider_code,
            "sp_payout_status": SpPayoutStatus.BLOCKED_MISSING_BANK,
            "blocked_amount_idr": ctx.sp_amount,
        }
        logger.warning(f"[Disbursement] SP_PAYOUT_BLOCKED {_blocked}")
        try:
            await order_collection.update_one(
                {"order_number": order_number},
                {"$set": {
                    "sp_payout_status":         SpPayoutStatus.BLOCKED_MISSING_BANK,
                    "sp_payout_blocked_reason": f"SP {ctx.service_provider_code} has no bank_code/account_number in sheet",
                    "sp_payout_amount":         ctx.sp_amount,
                    "payment.retryable":        True,
                }},
            )
        except Exception as _dbe:
            logger.error(f"[Disbursement] Failed to persist SP BLOCKED_BANK for {order_number}: {_dbe}")

    if not villa_bank_ready:
        _villa_status_val = PayoutStatus.BLOCKED_MISSING_BANK
        _blocked = {
            "order_id": order_number, "villa_code": ctx.villa_code,
            "payout_status": _villa_status_val, "blocked_amount_idr": ctx.villa_amount,
        }
        logger.warning(f"[Disbursement] VILLA_PAYOUT_BLOCKED {_blocked}")
        try:
            await order_collection.update_one(
                {"order_number": order_number},
                {"$set": {
                    "payout_status":         _villa_status_val,
                    "payout_blocked_reason": f"Villa {ctx.villa_code} has no bank_code/account_number in sheet",
                    "payout_blocked_amount": ctx.villa_amount,
                    "payment.retryable":     True,
                }},
            )
        except Exception as _dbe:
            logger.error(f"[Disbursement] Failed to persist VILLA BLOCKED_BANK for {order_number}: {_dbe}")

    # ── 5. Execute disbursements ─────────────────────────────────────────────
    sp_result   = {"success": False, "skipped": True, "reason": "missing_bank_details", "amount": ctx.sp_amount}
    villa_result = {"success": False, "skipped": True, "reason": "missing_bank_details", "amount": ctx.villa_amount}

    try:
        async with httpx.AsyncClient() as client:

            # ── SP disbursement ──────────────────────────────────────────────
            if sp_bank_ready:
                sp_result = await create_bank_disbursement(
                    client=client,
                    amount=ctx.sp_amount,
                    bank_details=sp_bank,
                    reference_id=f"sp_{order_number}",
                    description=build_payout_description(order_number, "service_provider"),
                    min_amount=XENDIT_MIN_DISBURSEMENT_IDR,
                )
            disbursements["service_provider"] = sp_result
            logger.info(
                f"[Disbursement] SP: order_id={order_number} sp_code={ctx.service_provider_code} "
                f"amount={ctx.sp_amount:,} success={sp_result.get('success')} "
                f"status={sp_result.get('status')} bank_ready={sp_bank_ready}"
            )

            # ── Villa disbursement ───────────────────────────────────────────
            villa_amount = ctx.villa_amount
            _test_active = XENDIT_ENV == "test" and VILLA_DISBURSEMENT_TEST_AMOUNT > 0
            if _test_active:
                logger.warning(
                    f"[Disbursement] TEST-MODE: overriding villa_amount {villa_amount:,} → "
                    f"{VILLA_DISBURSEMENT_TEST_AMOUNT:,} IDR"
                )
                villa_amount = VILLA_DISBURSEMENT_TEST_AMOUNT

            if villa_bank_ready:
                villa_result = await create_bank_disbursement(
                    client=client,
                    amount=villa_amount,
                    bank_details=villa_bank,
                    reference_id=f"villa_{order_number}",
                    description=build_payout_description(order_number, "villa"),
                    min_amount=XENDIT_VILLA_MIN_IDR,
                )
            disbursements["villa"] = villa_result
            logger.info(
                f"[Disbursement] Villa: order_id={order_number} villa_code={ctx.villa_code} "
                f"amount={villa_amount:,} success={villa_result.get('success')} "
                f"status={villa_result.get('status')} bank_ready={villa_bank_ready}"
            )

            disbursements["distributed_at"] = datetime.datetime.now()

    except Exception as e:
        logger.error(f"[Disbursement] Unexpected error for {order_number}: {e}")
        disbursements.setdefault("service_provider", {"success": False, "error": str(e)})
        disbursements.setdefault("villa", {"success": False, "error": str(e)})
        disbursements["distributed_at"] = datetime.datetime.now()

    # ── 6. Determine final status values ─────────────────────────────────────
    sp_ok    = disbursements.get("service_provider", {}).get("success", False)
    villa_ok = disbursements.get("villa", {}).get("success", False)
    sp_xendit_status  = disbursements.get("service_provider", {}).get("status")
    vl_xendit_status  = disbursements.get("villa", {}).get("status")

    # Overall order status
    if sp_xendit_status == "PENDING" or vl_xendit_status == "PENDING":
        final_status = "disbursement_pending"
    elif sp_ok or villa_ok:
        final_status = "disbursement_initiated"
    else:
        final_status = "distribution_failed"

    # Villa payout_status — preserve existing BLOCKED state if already set
    _order_current = await order_collection.find_one({"order_number": order_number}, {"payout_status": 1, "sp_payout_status": 1})
    _current_villa_payout = (_order_current or {}).get("payout_status", "")
    _current_sp_payout    = (_order_current or {}).get("sp_payout_status", "")

    if _current_villa_payout not in PayoutStatus.ALL_BLOCKED:
        _villa_payout_final = PayoutStatus.DISBURSED if villa_ok else (PayoutStatus.FAILED if not villa_bank_ready is False else PayoutStatus.FAILED)
        if not villa_ok and villa_bank_ready:
            _villa_payout_final = PayoutStatus.FAILED   # Xendit call attempted but failed
        elif not villa_ok and not villa_bank_ready:
            _villa_payout_final = _current_villa_payout  # already set BLOCKED above
        elif villa_ok:
            _villa_payout_final = PayoutStatus.DISBURSED
        else:
            _villa_payout_final = PayoutStatus.FAILED
    else:
        _villa_payout_final = _current_villa_payout

    if _current_sp_payout not in SpPayoutStatus.ALL_BLOCKED:
        if sp_ok:
            _sp_payout_final = SpPayoutStatus.DISBURSED
        elif sp_bank_ready:
            _sp_payout_final = SpPayoutStatus.FAILED     # Xendit call attempted but failed
        else:
            _sp_payout_final = _current_sp_payout        # already set BLOCKED_MISSING_BANK above
    else:
        _sp_payout_final = _current_sp_payout

    # ── 7. Persist all payout state atomically ────────────────────────────────
    _db_update: dict = {
        "payment.disbursements": disbursements,
        "status":               final_status,
        "payout_status":        _villa_payout_final,
        "sp_payout_status":     _sp_payout_final,
        "sp_payout_amount":     ctx.sp_amount,
        "payment.retryable":    final_status == "distribution_failed",
    }
    if sp_ok:
        _db_update["sp_payout_blocked_reason"] = None
    if villa_ok:
        _db_update["payout_blocked_reason"]  = None
        _db_update["payout_blocked_amount"]  = None

    try:
        await order_collection.update_one({"order_number": order_number}, {"$set": _db_update})
    except Exception as db_err:
        logger.error(f"[Disbursement] DB update failed for {order_number}: {db_err}")

    logger.info(
        f"[Disbursement] COMPLETE: order_id={order_number} "
        f"villa_code={ctx.villa_code} sp_code={ctx.service_provider_code} "
        f"sp_payout={_sp_payout_final} villa_payout={_villa_payout_final} "
        f"order_status={final_status} "
        f"SP={'OK' if sp_ok else 'BLOCKED/FAIL'} Villa={'OK' if villa_ok else 'BLOCKED/FAIL'}"
    )

    # ── 8. Admin alert — any blocked or failed payout triggers notification ───
    _sp_blocked   = _sp_payout_final in SpPayoutStatus.ALL_BLOCKED
    _villa_blocked = _villa_payout_final in PayoutStatus.ALL_BLOCKED
    _sp_failed_xendit   = not sp_ok and sp_bank_ready    # Xendit was called but failed
    _villa_failed_xendit = not villa_ok and villa_bank_ready

    if _sp_blocked or _villa_blocked or _sp_failed_xendit or _villa_failed_xendit:
        _sp_reason = (
            disbursements.get("service_provider", {}).get("reason")
            or disbursements.get("service_provider", {}).get("error")
            or _sp_payout_final
        )
        _vl_reason = (
            disbursements.get("villa", {}).get("reason")
            or disbursements.get("villa", {}).get("error")
            or _villa_payout_final
        )
        _cash_bal = disbursements.get("service_provider", {}).get("xendit_cash_balance")
        await _send_payout_admin_alert(
            order_number=order_number,
            sp_ok=sp_ok, villa_ok=villa_ok,
            sp_reason=_sp_reason, vl_reason=_vl_reason,
            cash_bal=_cash_bal,
        )

    return disbursements


async def _send_payout_admin_alert(
    *,
    order_number: str,
    sp_ok: bool,
    villa_ok: bool,
    sp_reason: str,
    vl_reason: str,
    cash_bal,
    extra_note: str = "",
) -> None:
    """Send WhatsApp alert to admin for any payout failure or block."""
    try:
        from app.utils.whatsapp_func import send_whatsapp_with_fallback
        import os
        admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
        balance_line = f"\n• Xendit CASH balance: IDR {int(cash_bal):,}" if cash_bal is not None else ""
        extra_line   = f"\n• {extra_note}" if extra_note else ""
        alert_msg = (
            f"⚠️ *Payout Issue — Manual Action Required*\n\n"
            f"*Order:* {order_number}\n"
            f"*SP payout:*    {'✅ sent' if sp_ok else f'❌ {sp_reason}'}\n"
            f"*Villa payout:* {'✅ sent' if villa_ok else f'❌ {vl_reason}'}"
            f"{balance_line}{extra_line}\n\n"
            f"Admin retry: POST /menu/retry-disbursements/{order_number}"
        )
        await send_whatsapp_with_fallback(
            admin_number, alert_msg, "admin_alert_seq",
            ["Payout Issue", order_number, f"Retry: /menu/retry-disbursements/{order_number}"],
            notification_type="admin_payout_alert",
            order_number=order_number,
        )
        logger.info(f"[Disbursement] Admin alert sent for {order_number}")
    except Exception as alert_err:
        logger.warning(f"[Disbursement] Admin alert failed for {order_number}: {alert_err}")
