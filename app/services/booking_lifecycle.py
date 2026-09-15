"""
booking_lifecycle.py — Automated lifecycle management for bookings.

Responsibilities:
1. Auto-complete: Marks CONFIRMED bookings as COMPLETED after 24 hours 
   from the scheduled service date/time.
2. Failed transitions: Handles cleanup of stuck or failed states.
"""

import asyncio
import datetime
from datetime import UTC
import logging
import os

from app.db.session import order_collection
from app.utils.logger import get_logger
from app.models.order_summary import BookingStatus
from app.services.status_service import BookingStatusManager

logger = get_logger("booking_lifecycle")

# ── Config ────────────────────────────────────────────────────────────────────
COMPLETION_WINDOW_HOURS = int(os.getenv("COMPLETION_WINDOW_HOURS", "24"))
LIFECYCLE_CHECK_INTERVAL = int(os.getenv("LIFECYCLE_CHECK_INTERVAL", "3600")) # 1 hour

async def sweep_booking_completions() -> int:
    """
    Find CONFIRMED bookings that are older than the completion window
    relative to their scheduled service date.
    """
    now = datetime.datetime.now(UTC)
    cutoff = now - datetime.timedelta(hours=COMPLETION_WINDOW_HOURS)

    # We look for CONFIRMED orders where 'date' (service date) is older than cutoff
    # If 'date' is missing, we use 'created_at' as fallback
    query = {
        "booking_status": BookingStatus.CONFIRMED,
        "$or": [
            {"date": {"$lte": cutoff}},
            {"date": None, "created_at": {"$lte": cutoff}}
        ]
    }

    completed_count = 0
    async for order in order_collection.find(query):
        order_number = order.get("order_number", "?")
        try:
            # Transition to COMPLETED
            # This will also trigger disbursement scheduling side-effects
            res = await BookingStatusManager.transition_booking_status(
                order_number=order_number,
                target_status=BookingStatus.COMPLETED,
                changed_by="SYSTEM_AUTO_COMPLETE",
                reason=f"Auto-completed {COMPLETION_WINDOW_HOURS}h after scheduled service date."
            )

            if res["success"]:
                completed_count += 1
                logger.info("BOOKING_LIFECYCLE", f"Auto-completed order {order_number}")
            else:
                logger.warning("BOOKING_LIFECYCLE", f"Failed to auto-complete {order_number}: {res.get('error')}")

        except Exception as e:
            logger.error("BOOKING_LIFECYCLE", f"Error processing completion for {order_number}: {e}")

    return completed_count

async def start_booking_lifecycle_worker():
    """Background loop for lifecycle tasks."""
    logger.info("BOOKING_LIFECYCLE", f"Worker started — window={COMPLETION_WINDOW_HOURS}h, interval={LIFECYCLE_CHECK_INTERVAL}s")
    
    while True:
        try:
            n = await sweep_booking_completions()
            if n > 0:
                logger.info("BOOKING_LIFECYCLE", f"Sweep complete: {n} bookings auto-completed.")
        except Exception as e:
            logger.error("BOOKING_LIFECYCLE", f"Worker loop error: {e}")
            
        await asyncio.sleep(LIFECYCLE_CHECK_INTERVAL)
