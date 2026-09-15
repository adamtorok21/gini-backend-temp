"""
sp_timeout_checker.py — Background sweep for bookings where no SP accepted.

Problem:
  When a booking is created, SPs are notified via WhatsApp template. If no SP
  responds (Accept/Decline), the order stays in status='pending' forever with no
  guest notification and no admin alert. The guest is left waiting indefinitely.

This service:
  - Runs every SP_CHECK_INTERVAL_SECONDS (default: 15 minutes)
  - Finds orders where sp_notified_at is older than SP_TIMEOUT_HOURS (default: 2h)
    AND confirmed_by_provider is None (no SP has accepted)
    AND status is still a pre-acceptance state
  - Marks them as status='sp_timeout'
  - Notifies the guest (WhatsApp or WebSocket)
  - Sends admin WhatsApp alert

Idempotency: uses find_one_and_update with status filter so concurrent loops
cannot double-process the same order.
"""

import asyncio
import datetime
import os

from app.db.session import order_collection
from app.utils.logger import get_logger
from app.models.order_summary import BookingStatus
from app.services.status_service import BookingStatusManager

logger = get_logger("sp_timeout")

# ── Config ────────────────────────────────────────────────────────────────────
SP_TIMEOUT_HOURS = float(os.getenv("SP_TIMEOUT_HOURS", "2.5"))        # hours until sp_timeout
SP_CHECK_INTERVAL_SECONDS = int(os.getenv("SP_CHECK_INTERVAL_SECONDS", "900"))  # 15 min


async def sweep_sp_timeouts() -> int:
    """
    Scan for stale bookings and mark them as sp_timeout.

    Returns the number of orders timed out in this sweep.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=SP_TIMEOUT_HOURS)

    query = {
        "booking_status":         BookingStatus.AWAITING_SP_CONFIRMATION,
        "confirmed_by_provider": None,           # no SP accepted
        "sp_notified_at":       {"$lte": cutoff, "$exists": True},
    }

    timed_out = 0
    async for order in order_collection.find(query):
        order_number = order.get("order_number", "?")
        try:
            # ── FSM Transition via BookingStatusManager ──────────────────────
            # This handles audit history, validation, and atomic status update.
            # Transition: AWAITING_SP_CONFIRMATION -> FAILED
            fsm_result = await BookingStatusManager.transition_booking_status(
                order_number=order_number,
                target_status=BookingStatus.FAILED,
                changed_by="SYSTEM_TIMEOUT",
                reason=f"No service provider accepted within {SP_TIMEOUT_HOURS}h timeout window"
            )

            if not fsm_result["success"]:
                # Race condition or transition validation failure
                logger.info("SP_TIMEOUT", f"FSM transition skipped for {order_number}: {fsm_result.get('error')}")
                continue

            # ── Additional Timeout Metadata ──────────────────────────────────
            # Update legacy display status and timeout timestamp.
            # Legacy 'status' is for display only; 'booking_status' is the truth.
            await order_collection.update_one(
                {"order_number": order_number},
                {
                    "$set": {
                        "status":          "sp_timeout",
                        "sp_timed_out_at": datetime.datetime.now(),
                    }
                }
            )

            timed_out += 1
            # transition_booking_status omits "order" in the no-change path
            # (order already in target status). Fall back to the swept doc so
            # notifications always have a valid order document.
            result_doc = fsm_result.get("order") or order
            logger.warning(
                "SP_TIMEOUT",
                f"Order {order_number} timed out after {SP_TIMEOUT_HOURS}h with no SP acceptance",
            )

            await _notify_guest(result_doc)
            await _notify_admin(result_doc)

        except Exception as e:
            logger.error("SP_TIMEOUT", f"Error processing {order_number}: {e}")

    return timed_out


async def _notify_guest(order: dict) -> None:
    """Send a timeout message to the guest (WhatsApp or WebSocket)."""
    try:
        sender_id = order.get("sender_id", "")
        order_number = order.get("order_number", "?")
        service_name = order.get("service_name", "your service")
        msg = (
            f"⚠️ *Booking Update — Order #{order_number}*\n\n"
            f"We were unable to find an available service provider for *{service_name}* "
            f"within the expected time.\n\n"
            f"We apologise for the inconvenience. Please contact us directly or "
            f"type *menu* to browse other options. No payment has been taken."
        )
        if sender_id.isdigit():
            from app.utils.whatsapp_func import send_whatsapp_with_fallback
            await send_whatsapp_with_fallback(
                sender_id, msg, "booking_cancelled_guest",
                [order_number],
                notification_type="sp_timeout_guest",
                order_number=order_number,
            )
        else:
            from app.services.websocket_managerr import ConnectionManager
            mgr = ConnectionManager()
            await mgr.send_personal_message(
                message=msg,
                session_id=sender_id,
                message_type="sp_timeout",
            )
    except Exception as e:
        logger.error("SP_TIMEOUT", f"Guest notification failed for {order.get('order_number')}: {e}")


async def _notify_admin(order: dict) -> None:
    """Send admin WhatsApp alert about the timed-out order."""
    try:
        from app.utils.whatsapp_func import send_whatsapp_with_fallback
        admin_number = os.getenv("ADMIN_WHATSAPP_NUMBER", "62895627705139")
        order_number = order.get("order_number", "?")
        alert_msg = (
            f"⏰ *SP Timeout — No Provider Accepted*\n\n"
            f"Order *{order_number}* has been waiting for SP acceptance for "
            f">{SP_TIMEOUT_HOURS:.0f}h with no response.\n\n"
            f"*Service:* {order.get('service_name', 'N/A')}\n"
            f"*Guest:* {order.get('customer_name') or order.get('sender_id', 'N/A')}\n"
            f"*Date:* {order.get('date') or order.get('booking_date', 'N/A')}\n\n"
            f"The guest has been notified. Manual reassignment may be required."
        )
        await send_whatsapp_with_fallback(
            admin_number, alert_msg, "admin_alert_seq",
            ["SP Timeout", order_number, "Manual reassignment required"],
            notification_type="admin_sp_timeout",
            order_number=order_number,
        )
    except Exception as e:
        logger.error("SP_TIMEOUT", f"Admin alert failed for {order.get('order_number')}: {e}")


async def start_sp_timeout_checker() -> None:
    """
    Background loop — registered at startup via asyncio.create_task().
    Runs every SP_CHECK_INTERVAL_SECONDS.
    """
    logger.info(
        "SP_TIMEOUT",
        f"Checker started — timeout={SP_TIMEOUT_HOURS}h, interval={SP_CHECK_INTERVAL_SECONDS}s",
    )
    while True:
        try:
            n = await sweep_sp_timeouts()
            if n:
                logger.warning("SP_TIMEOUT", f"Sweep complete: {n} order(s) timed out")
            else:
                logger.info("SP_TIMEOUT", "Sweep complete: 0 orders timed out")
        except Exception as e:
            logger.error("SP_TIMEOUT", f"Sweep error: {e}")

        await asyncio.sleep(SP_CHECK_INTERVAL_SECONDS)
