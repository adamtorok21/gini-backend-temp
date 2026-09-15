"""
payment_followup_worker.py — Add-on payment follow-up reminders (30-min + 1-hr).

This is an ADD-ON to the existing 10-minute reminder in payment_reminder_worker.py.
It does NOT modify, replace, or interfere with that worker. It only sends two
additional courtesy reminders and NEVER cancels a booking — Xendit's own invoice
expiry handles actual cancellation.

Timeline (all anchored on payment.link_sent_at — the same payment clock the
existing 10-min reminder uses):

    +10 min  →  payment_reminder_worker.py   (existing — untouched)
    +30 min  →  this worker, 2nd reminder     (flag: reminder_30min_sent)
    +60 min  →  this worker, final notice      (flag: final_notice_1hr_sent)

Design decisions (mirrors the existing worker for consistency):
- DB-state sweep, not asyncio.sleep per order — survives restarts/redeploys.
- Idempotency: find_one_and_update with a per-stage flag so concurrent worker
  instances or rapid restarts cannot double-send.
- Each stage uses an INDEPENDENT flag — the 30-min and 1-hr reminders never
  block each other, and neither touches the existing payment_reminder_sent flag.
- Reminder failure is logged but never crashes the worker or rolls back the flag.
- Old orders without payment.link_sent_at are excluded by $exists: True.
- No cancellation. No FSM transition. Notify only.
"""

import asyncio
import datetime
import os

from app.db.session import order_collection
from app.models.order_summary import BookingStatus, PaymentStatus
from app.utils.logger import get_logger

logger = get_logger("payment_followup")

REMINDER_30MIN_MINUTES = int(os.getenv("PAYMENT_FOLLOWUP_30MIN", "30"))
FINAL_NOTICE_60MIN_MINUTES = int(os.getenv("PAYMENT_FOLLOWUP_60MIN", "60"))
CHECK_INTERVAL_SECONDS = int(os.getenv("PAYMENT_FOLLOWUP_CHECK_INTERVAL", "120"))

_UNPAID_STATUSES = {"$nin": [PaymentStatus.PAID, PaymentStatus.EXPIRED, PaymentStatus.REFUNDED]}


async def _sweep_stage(minutes: int, flag_field: str, stage_label: str) -> int:
    """
    Generic sweep for one follow-up stage.

    Finds AWAITING_PAYMENT orders where the payment link is older than `minutes`,
    the per-stage flag is not set, and the order is still unpaid. Sends one
    reminder per order, claiming the flag atomically before sending.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=minutes)

    query = {
        "booking_status": BookingStatus.AWAITING_PAYMENT,
        "payment.link_sent_at": {"$exists": True, "$lte": cutoff},
        f"payment.{flag_field}": {"$ne": True},
        "fsm_payment_status": _UNPAID_STATUSES,
    }

    sent = 0
    async for order in order_collection.find(query):
        order_number = order.get("order_number", "?")
        try:
            payment_url = (order.get("payment") or {}).get("payment_url", "")
            if not payment_url:
                logger.warning(
                    "PAYMENT_FOLLOWUP",
                    f"[{stage_label}] Order {order_number} has no payment_url — skipping",
                )
                continue

            sender_id = order.get("sender_id", "")
            service_name = order.get("service_name", "your service")

            # Atomic idempotency lock — claim the per-stage flag before sending.
            claimed = await order_collection.find_one_and_update(
                {
                    "order_number": order_number,
                    f"payment.{flag_field}": {"$ne": True},
                },
                {
                    "$set": {
                        f"payment.{flag_field}": True,
                        f"payment.{flag_field}_at": datetime.datetime.now(),
                    }
                },
            )
            if claimed is None:
                logger.info(
                    "PAYMENT_FOLLOWUP",
                    f"[{stage_label}] Order {order_number} already claimed — skipping",
                )
                continue

            await _send_followup(sender_id, order_number, service_name, payment_url, stage_label)
            sent += 1
            logger.info(
                "PAYMENT_FOLLOWUP",
                f"[{stage_label}] Reminder sent for order {order_number} → {sender_id}",
            )

        except Exception as e:
            logger.error("PAYMENT_FOLLOWUP", f"[{stage_label}] Error processing {order_number}: {e}")

    return sent


async def _send_followup(
    sender_id: str,
    order_number: str,
    service_name: str,
    payment_url: str,
    stage_label: str,
) -> None:
    """Send the follow-up reminder via WhatsApp (with template fallback) or WebSocket."""
    if stage_label == "30min":
        message = (
            f"⏰ *Payment Reminder — Order #{order_number}*\n\n"
            f"Your booking for *{service_name}* is still reserved and waiting for payment.\n\n"
            f"Please complete your payment using the secure link below:\n\n"
            f"{payment_url}\n\n"
            f"If you no longer need this service, no action is required."
        )
    else:  # 1-hr final notice
        message = (
            f"⏳ *Final Payment Reminder — Order #{order_number}*\n\n"
            f"This is a final reminder for your *{service_name}* booking. "
            f"Your payment link is still active:\n\n"
            f"{payment_url}\n\n"
            f"Please complete payment soon to confirm your booking, "
            f"otherwise the link will expire automatically. "
            f"Need help? Contact us at +62 851-908-28581."
        )

    is_whatsapp = str(sender_id).isdigit()
    if is_whatsapp:
        from app.utils.whatsapp_func import send_whatsapp_with_fallback
        await send_whatsapp_with_fallback(
            recipient=sender_id,
            freeform_msg=message,
            template_name="payment_link_reminder",
            template_vars=[order_number, service_name, payment_url],
            notification_type=f"payment_followup_{stage_label}",
            order_number=order_number,
        )
    else:
        from app.services.websocket_managerr import ConnectionManager
        mgr = ConnectionManager()
        await mgr.send_personal_message(
            message=message,
            session_id=sender_id,
            message_type="payment_reminder",
        )


async def sweep_payment_followups() -> int:
    """Run both follow-up stages. Returns total reminders dispatched this sweep."""
    total = 0
    total += await _sweep_stage(REMINDER_30MIN_MINUTES, "reminder_30min_sent", "30min")
    total += await _sweep_stage(FINAL_NOTICE_60MIN_MINUTES, "final_notice_1hr_sent", "1hr")
    return total


async def start_payment_followup_worker() -> None:
    """
    Background loop — registered at startup via asyncio.create_task().
    Runs every CHECK_INTERVAL_SECONDS. Independent of the 10-min reminder worker.
    """
    logger.info(
        "PAYMENT_FOLLOWUP",
        f"Worker started — 30min stage={REMINDER_30MIN_MINUTES}min, "
        f"final stage={FINAL_NOTICE_60MIN_MINUTES}min, interval={CHECK_INTERVAL_SECONDS}s",
    )
    while True:
        try:
            n = await sweep_payment_followups()
            if n:
                logger.info("PAYMENT_FOLLOWUP", f"Sweep complete: {n} follow-up(s) sent")
            else:
                logger.info("PAYMENT_FOLLOWUP", "Sweep complete: 0 follow-ups needed")
        except Exception as e:
            logger.error("PAYMENT_FOLLOWUP", f"Worker loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
