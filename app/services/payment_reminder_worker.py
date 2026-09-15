"""
payment_reminder_worker.py — 10-minute payment reminder sweep.

After a service provider accepts a booking and the guest receives the payment link,
if the guest has not paid within PAYMENT_REMINDER_MINUTES (default: 10), this worker
sends a single reminder with the original payment URL.

Design decisions:
- DB-state sweep, not asyncio.sleep per order — survives server restarts/redeploys.
- Runs every CHECK_INTERVAL_SECONDS (default: 120s / 2 min).
- Idempotency: uses find_one_and_update with {payment_reminder_sent: {$ne: True}} so
  concurrent worker instances or rapid restarts cannot double-send.
- Reminder failure is logged but never crashes the worker or rolls back the flag.
- Old orders without payment.link_sent_at are excluded by the $exists: True query.
"""

import asyncio
import datetime
import os

from app.db.session import order_collection
from app.models.order_summary import BookingStatus, PaymentStatus
from app.utils.logger import get_logger

logger = get_logger("payment_reminder")

PAYMENT_REMINDER_MINUTES = int(os.getenv("PAYMENT_REMINDER_MINUTES", "10"))
# Second (final) reminder — minutes after link_sent_at, i.e. 30 min after the
# first reminder. The payment link itself dies at 60 min (duration setting in
# config.py), so this message warns the guest the link expires in ~20 minutes.
PAYMENT_REMINDER2_MINUTES = int(os.getenv("PAYMENT_REMINDER2_MINUTES", "40"))
CHECK_INTERVAL_SECONDS = int(os.getenv("PAYMENT_REMINDER_CHECK_INTERVAL", "120"))


async def sweep_payment_reminders() -> int:
    """
    Find orders in AWAITING_PAYMENT where the payment link was sent >10min ago
    and no reminder has been sent yet. Send one reminder per order.

    Returns the number of reminders dispatched in this sweep.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(minutes=PAYMENT_REMINDER_MINUTES)

    query = {
        "booking_status": BookingStatus.AWAITING_PAYMENT,
        "payment.link_sent_at": {"$exists": True, "$lte": cutoff},
        "payment.payment_reminder_sent": {"$ne": True},
        "fsm_payment_status": {"$nin": [PaymentStatus.PAID, PaymentStatus.EXPIRED, PaymentStatus.REFUNDED]},
    }

    reminded = 0
    async for order in order_collection.find(query):
        order_number = order.get("order_number", "?")
        try:
            payment_url = (order.get("payment") or {}).get("payment_url", "")
            if not payment_url:
                logger.warning(
                    "PAYMENT_REMINDER",
                    f"Order {order_number} has no payment_url — skipping reminder",
                )
                continue

            sender_id = order.get("sender_id", "")
            service_name = order.get("service_name", "your service")

            # Atomic idempotency lock — mark before sending so a crash/restart
            # cannot send a second reminder for the same order.
            claimed = await order_collection.find_one_and_update(
                {
                    "order_number": order_number,
                    "payment.payment_reminder_sent": {"$ne": True},
                },
                {
                    "$set": {
                        "payment.payment_reminder_sent": True,
                        "payment.reminder_sent_at": datetime.datetime.now(),
                    }
                },
            )
            if claimed is None:
                # A concurrent sweep already claimed this order.
                logger.info("PAYMENT_REMINDER", f"Order {order_number} already claimed — skipping")
                continue

            await _send_reminder(sender_id, order_number, service_name, payment_url)
            reminded += 1
            logger.info("PAYMENT_REMINDER", f"Reminder sent for order {order_number} → {sender_id}")

        except Exception as e:
            logger.error("PAYMENT_REMINDER", f"Error processing {order_number}: {e}")

    # ── Second (final) reminder — 40 min after the link, ~20 min before expiry ──
    cutoff2 = datetime.datetime.now() - datetime.timedelta(minutes=PAYMENT_REMINDER2_MINUTES)
    query2 = {
        "booking_status": BookingStatus.AWAITING_PAYMENT,
        "payment.link_sent_at": {"$exists": True, "$lte": cutoff2},
        "payment.payment_reminder_sent": True,
        "payment.payment_reminder2_sent": {"$ne": True},
        "fsm_payment_status": {"$nin": [PaymentStatus.PAID, PaymentStatus.EXPIRED, PaymentStatus.REFUNDED]},
    }

    async for order in order_collection.find(query2):
        order_number = order.get("order_number", "?")
        try:
            payment_url = (order.get("payment") or {}).get("payment_url", "")
            if not payment_url:
                continue

            sender_id = order.get("sender_id", "")
            service_name = order.get("service_name", "your service")

            # Same atomic claim pattern as the first reminder — flag set before
            # sending so a crash/restart/concurrent sweep cannot double-send.
            claimed = await order_collection.find_one_and_update(
                {
                    "order_number": order_number,
                    "payment.payment_reminder2_sent": {"$ne": True},
                },
                {
                    "$set": {
                        "payment.payment_reminder2_sent": True,
                        "payment.reminder2_sent_at": datetime.datetime.now(),
                    }
                },
            )
            if claimed is None:
                continue

            await _send_final_reminder(sender_id, order_number, service_name, payment_url)
            reminded += 1
            logger.info("PAYMENT_REMINDER", f"Final reminder sent for order {order_number} → {sender_id}")

        except Exception as e:
            logger.error("PAYMENT_REMINDER", f"Error processing final reminder {order_number}: {e}")

    return reminded


async def _send_reminder(
    sender_id: str,
    order_number: str,
    service_name: str,
    payment_url: str,
) -> None:
    reminder_message = (
        f"⏰ *Payment Reminder — Order #{order_number}*\n\n"
        f"Your booking for *{service_name}* is reserved and waiting for payment.\n\n"
        f"Please complete your payment using the secure link below to confirm your booking:\n\n"
        f"{payment_url}\n\n"
        f"If you no longer need this service, no action is required — "
        f"the link will expire automatically."
    )

    is_whatsapp = str(sender_id).isdigit()
    if is_whatsapp:
        from app.utils.whatsapp_func import send_whatsapp_with_fallback
        await send_whatsapp_with_fallback(
            recipient=sender_id,
            freeform_msg=reminder_message,
            template_name="payment_link_reminder",
            template_vars=[order_number, service_name, payment_url],
            notification_type="payment_link_reminder",
            order_number=order_number,
        )
    else:
        from app.services.websocket_managerr import ConnectionManager
        mgr = ConnectionManager()
        await mgr.send_personal_message(
            message=reminder_message,
            session_id=sender_id,
            message_type="payment_reminder",
        )


async def _send_final_reminder(
    sender_id: str,
    order_number: str,
    service_name: str,
    payment_url: str,
) -> None:
    final_message = (
        f"⏰ *Final Payment Reminder — Order #{order_number}*\n\n"
        f"Your booking for *{service_name}* is still waiting for payment.\n\n"
        f"⚠️ Your payment link will expire in about *20 minutes*. After that, "
        f"this booking will be automatically cancelled.\n\n"
        f"Complete your payment now to keep your booking:\n\n"
        f"{payment_url}"
    )

    is_whatsapp = str(sender_id).isdigit()
    if is_whatsapp:
        from app.utils.whatsapp_func import send_whatsapp_with_fallback
        await send_whatsapp_with_fallback(
            recipient=sender_id,
            freeform_msg=final_message,
            template_name="payment_link_reminder",
            template_vars=[order_number, service_name, payment_url],
            notification_type="payment_link_final_reminder",
            order_number=order_number,
        )
    else:
        from app.services.websocket_managerr import ConnectionManager
        mgr = ConnectionManager()
        await mgr.send_personal_message(
            message=final_message,
            session_id=sender_id,
            message_type="payment_reminder",
        )


async def start_payment_reminder_worker() -> None:
    """
    Background loop — registered at startup via asyncio.create_task().
    Runs every CHECK_INTERVAL_SECONDS.
    """
    logger.info(
        "PAYMENT_REMINDER",
        f"Worker started — reminder_after={PAYMENT_REMINDER_MINUTES}min, "
        f"interval={CHECK_INTERVAL_SECONDS}s",
    )
    while True:
        try:
            n = await sweep_payment_reminders()
            if n:
                logger.info("PAYMENT_REMINDER", f"Sweep complete: {n} reminder(s) sent")
            else:
                logger.info("PAYMENT_REMINDER", "Sweep complete: 0 reminders needed")
        except Exception as e:
            logger.error("PAYMENT_REMINDER", f"Worker loop error: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
