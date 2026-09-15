import logging
import asyncio
import datetime
from typing import Dict, Any, Optional
from app.models.order_summary import Order, DisbursementStatus, BookingStatus, PaymentStatus
from app.settings.config import settings
from app.services.payment_service import create_bank_disbursement
from app.services.status_service import BookingStatusManager
from app.utils.whatsapp_func import send_villa_commission_notification
import httpx

logger = logging.getLogger(__name__)

async def execute_payouts(order_doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 3C-3A: Execute Xendit payouts for SP and Villa if feature flag is ON.
    Feature Flag: ENABLE_LIVE_DISBURSEMENT (default: False)
    """
    order_number = order_doc.get("order_number")
    
    # 1. Guardrails
    if order_doc.get("disbursement_status") != DisbursementStatus.PENDING:
        logger.warning(f"[Executor] Order {order_number} is not PENDING. Skipping.")
        return {"success": False, "reason": "not_pending"}
        
    if order_doc.get("booking_status") != BookingStatus.COMPLETED:
        logger.warning(f"[Executor] Order {order_number} is not COMPLETED. Skipping.")
        return {"success": False, "reason": "not_completed"}

    if order_doc.get("fsm_payment_status") != PaymentStatus.PAID:
        logger.warning(f"[Executor] Order {order_number} is not PAID. Skipping.")
        return {"success": False, "reason": "not_paid"}
        
    if order_doc.get("is_locked"):
        logger.warning(f"[Executor] Order {order_number} is LOCKED. Skipping.")
        return {"success": False, "reason": "locked"}
        
    simulation = order_doc.get("disbursement_simulation", {})
    if not simulation or not simulation.get("overall_ready"):
        logger.warning(f"[Executor] Order {order_number} simulation is NOT READY. Skipping.")
        return {"success": False, "reason": "simulation_not_ready"}
        
    # 2. Feature Flag Check
    if not settings.ENABLE_LIVE_DISBURSEMENT:
        logger.info(f"[Executor] LIVE DISBURSEMENT DISABLED for {order_number} (Dry Run).")
        return {"success": True, "dry_run": True}
        
    # 3. Live Execution
    logger.info(f"[Executor] STARTING LIVE EXECUTION for {order_number}")
    
    current_attempts = order_doc.get("disbursement_attempt_count", 0)
    now = datetime.datetime.now()
    
    results = {"sp": None, "villa": None}
    sp_payload = simulation.get("sp", {}).get("payload")
    villa_payload = simulation.get("villa", {}).get("payload")

    # EB share must never be sent
    if "eb" in simulation:
        logger.error(f"[Executor] CRITICAL: Simulation for {order_number} contains EB share payout. ABORTING.")
        return {"success": False, "error": "EB share detected in simulation"}
    
    async with httpx.AsyncClient() as client:
        # Payout Leg 1: Service Provider
        if sp_payload:
            logger.info(f"[Executor] Executing SP payout for {order_number} (Amount: {sp_payload['amount']})")
            results["sp"] = await create_bank_disbursement(
                client=client,
                amount=sp_payload["amount"],
                bank_details={
                    "bank_code": sp_payload["bank_code"],
                    "account_number": sp_payload["account_number"],
                    "account_holder_name": sp_payload["account_holder_name"]
                },
                reference_id=sp_payload["external_id"],
                description=sp_payload["description"]
            )
            
        # Payout Leg 2: Villa
        if villa_payload:
            logger.info(f"[Executor] Executing Villa payout for {order_number} (Amount: {villa_payload['amount']})")
            results["villa"] = await create_bank_disbursement(
                client=client,
                amount=villa_payload["amount"],
                bank_details={
                    "bank_code": villa_payload["bank_code"],
                    "account_number": villa_payload["account_number"],
                    "account_holder_name": villa_payload["account_holder_name"]
                },
                reference_id=villa_payload["external_id"],
                description=villa_payload["description"]
            )
            
    # 4. Final State Update
    sp_ok = results["sp"].get("success") if results["sp"] else True
    villa_ok = results["villa"].get("success") if results["villa"] else True
    
    extra_fields = {
        "disbursement_attempt_count": current_attempts + 1,
        "last_disbursement_attempt_at": now,
        "last_disbursement_results": results
    }

    if sp_ok and villa_ok:
        logger.info(f"[Executor] SUCCESS: All payouts executed for {order_number}.")
        await BookingStatusManager.transition_disbursement_status(
            order_number=order_number,
            target_status=DisbursementStatus.DISTRIBUTED,
            changed_by="SYSTEM_DISBURSEMENT_EXECUTOR",
            reason="Live payouts executed successfully.",
            extra_fields=extra_fields
        )
        # Notify villa of their commission now that the bank transfer is confirmed.
        # Skip for test/simulation orders — same guard pattern as send_whatsapp_order_to_SP.
        _is_test_order = (
            order_doc.get("is_test") is True
            or str(order_doc.get("sender_id", "")).startswith(("test_", "sim_"))
            or str(order_doc.get("order_number", "")).startswith(("TEST-", "W-PIPE-", "SIM-"))
        )
        if _is_test_order:
            logger.info(f"[Executor] Skipping villa commission notification for test order {order_number}")
        else:
            try:
                await send_villa_commission_notification(order_doc, results)
            except Exception as _vn_err:
                logger.error(f"[Executor] Villa commission notification failed for {order_number}: {_vn_err}")
        return {"success": True, "results": results}
    else:
        # One or both legs failed
        error_parts = []
        if results["sp"] and not results["sp"].get("success"):
            error_parts.append(f"SP: {results['sp'].get('error')}")
        if results["villa"] and not results["villa"].get("success"):
            error_parts.append(f"Villa: {results['villa'].get('error')}")
            
        error_msg = "; ".join(error_parts)
        logger.error(f"[Executor] FAILURE: Payout execution failed for {order_number}. Errors: {error_msg}")
        
        extra_fields["disbursement_error"] = error_msg
        
        await BookingStatusManager.transition_disbursement_status(
            order_number=order_number,
            target_status=DisbursementStatus.FAILED,
            changed_by="SYSTEM_DISBURSEMENT_EXECUTOR",
            reason=f"Live payout execution failed. {error_msg}",
            extra_fields=extra_fields
        )
        return {"success": False, "error": error_msg, "results": results}
