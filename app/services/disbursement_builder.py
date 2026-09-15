import logging
from typing import Dict, Any, Optional
from app.models.order_summary import Order
from app.services.payment_service import (
    XENDIT_MIN_DISBURSEMENT_IDR,
    XENDIT_VILLA_MIN_IDR,
    build_payout_description
)

logger = logging.getLogger(__name__)

def _reconciliation_error(dist_data: Dict[str, Any]) -> Optional[str]:
    """RECON-PIPE-01 (2026-08-26): narrow reconciliation check only — does
    NOT reuse the full PayoutContext.build() gate. That gate also requires
    location_zone and strict SP/villa code formats, fields unrelated to
    whether the split arithmetic is trustworthy; an earlier version of this
    fix called PayoutContext.build() directly and broke
    test_disbursement_negative.py, whose base_order has no location_zone —
    a real, previously-valid order shape that has nothing to do with
    reconciliation. This function checks ONLY that SP+Villa+EB sums to
    total_distribution (±1 IDR tolerance for sheet-math rounding, matching
    PayoutContext's own tolerance), the same math PayoutContext.build()
    performs, without adopting its unrelated stricter field requirements.

    Returns an error message string if the split does not reconcile or
    total_distribution is absent, None if it reconciles cleanly (or there
    is nothing to reconcile, e.g. distribution_data missing entirely — that
    case is caught by build_disbursement_payloads' existing per-leg checks).
    """
    if not dist_data:
        return None
    try:
        sp_amount = int(dist_data.get("service_provider", {}).get("amount", 0))
        villa_amount = int(dist_data.get("villa", {}).get("amount", 0))
        eb_amount = int(dist_data.get("easy_bali", {}).get("amount", 0))
    except (TypeError, ValueError) as e:
        return f"Unparseable amounts in distribution_data: {e}"

    if "total_distribution" not in dist_data:
        # No total to reconcile against — nothing to check here; the
        # per-leg zero/missing-bank-details checks below still apply.
        return None

    try:
        total_dist = int(dist_data["total_distribution"])
    except (TypeError, ValueError) as e:
        return f"Unparseable total_distribution in distribution_data: {e}"

    computed_total = sp_amount + villa_amount + eb_amount
    tolerance = 1  # 1 IDR: acceptable integer rounding from sheet math — matches PayoutContext
    if abs(computed_total - total_dist) > tolerance:
        return (
            f"Split amounts do not reconcile: SP={sp_amount:,} + Villa={villa_amount:,} "
            f"+ EB={eb_amount:,} = {computed_total:,} ≠ total_distribution={total_dist:,}. "
            f"Difference: {abs(computed_total - total_dist):,} IDR."
        )
    return None


async def build_disbursement_payloads(order_doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 3C-2B: Build Xendit disbursement payloads for SP and Villa.
    Does NOT call Xendit. Returns validation results and payloads.

    RECON-PIPE-01 (2026-08-26): before building any payload, validate the
    order's distribution_data reconciles (SP+Villa+EB == total_distribution,
    within tolerance) — see _reconciliation_error() above for why this is a
    narrow, standalone check rather than a reuse of the full
    PayoutContext.build() gate the manual retry path uses. Without this, a
    distribution_data dict that is present but no longer reconciles (e.g.
    edited after invoice creation) could still be paid out here — this is
    the automated pipeline that actually executes payouts in production.

    Missing bank details are reported via results["sp"/"villa"]["errors"]
    as before (unchanged behavior) AND now trigger a one-time admin alert —
    previously an order with missing bank details simply sat at PENDING
    forever with no one told why.
    """
    from app.routes.main_menu_routes import (
        get_bank_details_for_provider,
        get_bank_details_for_villa
    )

    order_number = order_doc.get("order_number")
    sp_code = order_doc.get("service_provider_code")
    villa_code = order_doc.get("villa_code")

    # Extract distribution data
    payment_info = order_doc.get("payment", {})
    dist_data = payment_info.get("distribution_data", {})

    sp_dist = dist_data.get("service_provider", {})
    villa_dist = dist_data.get("villa", {})

    sp_amount = sp_dist.get("amount", 0)
    villa_amount = villa_dist.get("amount", 0)

    # Stable references from Phase 3C-2A
    sp_ref = order_doc.get("sp_disbursement_reference") or f"{order_number}-SP"
    villa_ref = order_doc.get("villa_disbursement_reference") or f"{order_number}-VL"

    results = {
        "order_number": order_number,
        "sp": {"ready": False, "payload": None, "errors": []},
        "villa": {"ready": False, "payload": None, "errors": []},
        "overall_ready": False
    }

    # ── RECON-PIPE-01: reconciliation guard, before any payload is built ──────
    recon_error = _reconciliation_error(dist_data)
    if recon_error:
        results["sp"]["errors"].append(recon_error)
        results["villa"]["errors"].append(recon_error)
        results["overall_ready"] = False
        logger.error(f"[Disbursement] {order_number} blocked at build time: {recon_error}")
        await _send_disbursement_blocked_alert(order_number, recon_error)
        return results

    # 1. Build Service Provider Payload
    if sp_code:
        try:
            sp_bank = await get_bank_details_for_provider(sp_code)
            
            # Validations
            if not sp_bank.get("bank_code") or not sp_bank.get("account_number"):
                results["sp"]["errors"].append(f"Missing bank details for SP {sp_code}")
            
            if sp_amount <= 0:
                results["sp"]["errors"].append(f"SP payout amount must be > 0 (got {sp_amount})")
            elif XENDIT_MIN_DISBURSEMENT_IDR > 0 and sp_amount < XENDIT_MIN_DISBURSEMENT_IDR:
                results["sp"]["errors"].append(f"SP amount {sp_amount} below minimum {XENDIT_MIN_DISBURSEMENT_IDR}")
            
            if not results["sp"]["errors"]:
                results["sp"]["ready"] = True
                results["sp"]["payload"] = {
                    "external_id": sp_ref,
                    "amount": int(sp_amount),
                    "bank_code": sp_bank.get("bank_code"),
                    "account_holder_name": sp_bank.get("account_holder_name") or sp_bank.get("account_name", ""),
                    "account_number": sp_bank.get("account_number"),
                    "description": build_payout_description(order_number, "service_provider")
                }
        except Exception as e:
            results["sp"]["errors"].append(f"Error fetching SP bank details: {str(e)}")
    else:
        results["sp"]["errors"].append("Missing service_provider_code")

    # 2. Build Villa Payload
    if villa_code:
        try:
            villa_bank = await get_bank_details_for_villa(villa_code)
            
            # Validations
            if not villa_bank.get("bank_code") or not villa_bank.get("account_number"):
                results["villa"]["errors"].append(f"Missing bank details for Villa {villa_code}")
            
            if villa_amount <= 0:
                results["villa"]["errors"].append(f"Villa payout amount must be > 0 (got {villa_amount})")
            elif XENDIT_VILLA_MIN_IDR > 0 and villa_amount < XENDIT_VILLA_MIN_IDR:
                results["villa"]["errors"].append(f"Villa amount {villa_amount} below minimum {XENDIT_VILLA_MIN_IDR}")
            
            if not results["villa"]["errors"]:
                results["villa"]["ready"] = True
                results["villa"]["payload"] = {
                    "external_id": villa_ref,
                    "amount": int(villa_amount),
                    "bank_code": villa_bank.get("bank_code"),
                    "account_holder_name": villa_bank.get("account_holder_name") or villa_bank.get("name_of_villa", ""),
                    "account_number": villa_bank.get("account_number"),
                    "description": build_payout_description(order_number, "villa")
                }
        except Exception as e:
            results["villa"]["errors"].append(f"Error fetching Villa bank details: {str(e)}")
    else:
        results["villa"]["errors"].append("Missing villa_code")

    # Final overall readiness check
    results["overall_ready"] = results["sp"]["ready"] and results["villa"]["ready"]

    # RECON-PIPE-01: previously an order blocked here (e.g. missing bank
    # details) simply stayed at PENDING forever with disbursement_simulation
    # populated but nothing surfacing the reason to an admin — the manual
    # retry path has always alerted for this exact condition; the automated
    # pipeline never did. Alert once, at build time, for either leg's errors.
    if not results["overall_ready"]:
        blocked_reasons = results["sp"]["errors"] + results["villa"]["errors"]
        if blocked_reasons:
            await _send_disbursement_blocked_alert(order_number, "; ".join(blocked_reasons))

    return results


async def _send_disbursement_blocked_alert(order_number: Optional[str], reason: str) -> None:
    """Non-fatal admin alert for an order that could not be prepared for
    disbursement. Mirrors the existing _send_payout_admin_alert pattern in
    payment_service.py (manual retry path) — this is the automated-pipeline
    equivalent, since SWEEP 1 in automation_butler.py calls
    build_disbursement_payloads() exactly once per order at the moment it
    crosses SCHEDULED -> PENDING, so this fires once, not on every sweep."""
    try:
        from app.utils.whatsapp_func import send_whatsapp_with_fallback
        import os
        admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
        alert_msg = (
            f"⚠️ *Disbursement Blocked — Manual Action Required*\n\n"
            f"*Order:* {order_number}\n"
            f"*Reason:* {reason}\n\n"
            f"Fix the underlying issue, then: POST /menu/retry-disbursements/{order_number}"
        )
        await send_whatsapp_with_fallback(
            admin_number, alert_msg, "admin_alert_seq",
            ["Disbursement Blocked", str(order_number), f"Retry: /menu/retry-disbursements/{order_number}"],
            notification_type="admin_disbursement_blocked_alert",
            order_number=order_number,
        )
        logger.info(f"[Disbursement] Blocked-alert sent for {order_number}")
    except Exception as alert_err:
        logger.warning(f"[Disbursement] Blocked-alert failed for {order_number}: {alert_err}")
