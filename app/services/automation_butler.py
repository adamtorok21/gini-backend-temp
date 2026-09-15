from app.db.session import db
from app.services.whatsapp_queue import enqueue_whatsapp_template
from app.services.menu_services import get_villa_info_by_code
from datetime import datetime, timedelta, time
import asyncio
import logging
import re
from dateutil import parser as date_parser
from app.models.order_summary import PaymentStatus, BookingStatus, DisbursementStatus

logger = logging.getLogger(__name__)

order_collection      = db["orders-summary"]
checkin_collection    = db["checkins"]
passport_collection   = db["passports"]
template_collection   = db["message_templates"]
guest_reg_collection  = db["guest_registrations"]
automation_log_col     = db["automation_logs"]

# ── Hardcoded fallback defaults (used when DB template is inactive/missing) ───

_DEFAULTS = {
    "passport_reminder": (
        "👋 *A Friendly Reminder*\n\n"
        "We noticed you haven't submitted your passport yet. It's required for local registration.\n\n"
        "Please upload a clear photo of your passport here on WhatsApp. 📄"
    ),
    "day_1_welcome": (
        "🌴 *Welcome to your first full day!*\n\n"
        "We hope you're settling in perfectly. If you need fresh towels, pool cleaning, or local tips, just let me know!\n\n"
        "Have a wonderful day in Bali. ✨"
    ),
    "mid_stay_concierge": (
        "☀️ *Time for an adventure?*\n\n"
        "You've been with us for a few days now! If you'd like to book a private tour, a spa treatment, or a driver for dinner, I'm here to help.\n\n"
        "Just type *Order Services* to explore our curated Bali experiences. 🥥"
    ),
    "pre_checkout_remind": (
        "👋 *Planning your departure?*\n\n"
        "We hope you've enjoyed your stay! Your checkout is coming up soon. 🕑\n\n"
        "Would you like us to arrange an *Airport Transfer* or a driver for your next stop?\n"
        "Type *Order Services* to book your ride now."
    ),
    "feedback_request": (
        "⭐ *We'd love your feedback!*\n\n"
        "Thank you for choosing GINI Bali. On a scale of *1–5*, how was your experience at the villa and with our concierge service?\n\n"
        "Your feedback helps us provide a better experience for future guests! 🙏"
    ),
    "pre_arrival": (
        "🌺 *Your Bali stay begins soon!*\n\n"
        "Hi {name}, we are getting your villa ready for your arrival!\n\n"
        "Here's what to expect:\n"
        "• Scan the QR code at the villa entrance to check in\n"
        "• WiFi details and orientation guide will be shared on arrival\n"
        "• Your AI concierge activates the moment you check in\n"
        "• Directions: {maps_link}\n\n"
        "What time do you expect to arrive? Reply with your ETA (e.g., *3 PM*). See you soon! 🥥"
    ),
}


# ── Template fetcher ──────────────────────────────────────────────────────────

async def get_template(villa_code: str, trigger_type: str) -> str:
    """
    Return active template content for (villa_code, trigger_type).
    Falls back to hardcoded default if not found or marked inactive.
    """
    tpl = await template_collection.find_one({"villa_code": villa_code, "type": trigger_type})
    if tpl and tpl.get("is_active", False):
        return tpl["content"]
    return _DEFAULTS.get(trigger_type, "")


def _apply_vars(msg: str, sender_id: str = "", guest_name: str = "", maps_link: str = "") -> str:
    name = guest_name or f"Guest ...{sender_id[-4:]}" if sender_id else "Guest"
    msg = msg.replace("{name}", name)
    if maps_link:
        msg = msg.replace("{maps_link}", maps_link)
    else:
        msg = msg.replace("\n• Directions: {maps_link}", "")
    return msg


# ── Main background loop ──────────────────────────────────────────────────────

async def process_automations():
    """Background worker — runs every hour."""
    while True:
        try:
            now = datetime.now()
            logger.info("🤖 Butler: Scanning for guest automations...")

            # 1. Pre-arrival (before guests arrive)
            await check_pre_arrival_triggers(now)

            # 2. Active check-ins
            async for checkin in checkin_collection.find({"status": "active"}):
                await check_checkin_triggers(checkin, now)

            # 3. Order lifecycle: Auto-completion (Phase 3A)
            await check_order_auto_completion(now)

            # Phase 3C-1: Process scheduled disbursements
            await check_scheduled_disbursements(now)

        except Exception as e:
            logger.error(f"Butler Error: {e}")

        await asyncio.sleep(3600)


# ── Pre-arrival: scan guest_registrations ────────────────────────────────────

async def check_pre_arrival_triggers(now: datetime):
    """Send pre-arrival WhatsApp 24–48 hours before registered check-in date."""
    async for reg in guest_reg_collection.find({"status": "expected", "pre_arrival_sent": {"$ne": True}}):
        sender_id  = reg.get("sender_id")
        villa_code = reg.get("villa_code")
        checkin_dt = reg.get("checkin_date")
        guest_name = reg.get("guest_name", "")

        if not sender_id or not checkin_dt:
            continue

        hours_until = (checkin_dt - now).total_seconds() / 3600
        if 0 < hours_until <= 72:
            villa_info = await get_villa_info_by_code(villa_code) or {}
            maps_link = (
                villa_info.get("map_link")
                or villa_info.get("directions")
                or "See you at the villa!"
            )
            guest_display = guest_name or f"Guest ...{sender_id[-4:]}"
            await enqueue_whatsapp_template(
                sender_id, "pre_arrival_seq", [guest_display, maps_link]
            )
            await automation_log_col.insert_one({
                "guest_id": sender_id,
                "event_type": "pre_arrival",
                "sent_at": now,
                "villa_code": villa_code
            })
            await guest_reg_collection.update_one(
                {"_id": reg["_id"]},
                {"$set": {
                    "pre_arrival_sent": True,
                    "pre_arrival_sent_at": now,
                    "awaiting_eta": True,
                }}
            )
            logger.info(f"Butler: pre_arrival sent to {sender_id} ({villa_code})")


# ── Check-in lifecycle triggers ───────────────────────────────────────────────

async def _claim_sequence(checkin_id, trigger_name: str, guest_id: str = None) -> bool:
    """Atomically claim a sequence for THIS run: add it to sent_automations only if
    it is not already there. Returns True only if this call performed the write —
    i.e. this run/instance won the claim and should send. If another concurrent
    butler run (or another Render instance) already claimed it, returns False and
    the caller must NOT send. This prevents duplicate sequence messages (e.g. the
    day-1 welcome firing twice). When guest_id is given and the claim succeeds, an
    automation_log row is written."""
    res = await checkin_collection.update_one(
        {"_id": checkin_id, "sent_automations": {"$ne": trigger_name}},
        {"$addToSet": {"sent_automations": trigger_name}},
    )
    claimed = res.modified_count == 1
    if claimed and guest_id:
        await automation_log_col.insert_one({
            "guest_id": guest_id,
            "event_type": trigger_name,
            "sent_at": datetime.now(),
            "checkin_id": str(checkin_id),
        })
    return claimed


async def check_checkin_triggers(checkin: dict, now: datetime):
    sender_id  = checkin.get("sender_id")
    villa_code = checkin.get("villa_code")
    checkin_time = checkin.get("checkin_time")   # QR scan moment — always present
    sent = checkin.get("sent_automations", [])

    if not checkin_time or not sender_id:
        return

    # Use guest-provided checkin_date if onboarding completed, else fall back to QR scan time
    effective_checkin = checkin.get("checkin_date") or checkin_time

    # Each sequence fires within a bounded window. A backdated / mid-stay check-in
    # (effective_checkin already well in the past) must NOT dump every past-due
    # sequence at once — instead the stale ones are marked sent WITHOUT sending.

    # 0. On-arrival welcome — 0-24h after the check-in date (fires before day-1).
    #    Arrival support activation + villa-info guidance for the just-arrived guest.
    if "on_arrival" not in sent:
        elapsed = now - effective_checkin
        if timedelta(0) <= elapsed <= timedelta(hours=24):
            if await _claim_sequence(checkin["_id"], "on_arrival", sender_id):
                guest_display = checkin.get("guest_name") or f"Guest ...{sender_id[-4:]}"
                await enqueue_whatsapp_template(
                    sender_id, "on_arrival_seq", [guest_display]
                )
        elif elapsed > timedelta(hours=24):
            await _claim_sequence(checkin["_id"], "on_arrival")  # stale — skip

    # 1. Passport reminder — 1h after QR scan, only within a fresh 72h window.
    if "passport_reminder" not in sent:
        elapsed_scan = now - checkin_time
        if timedelta(hours=1) < elapsed_scan <= timedelta(hours=72):
            existing = await passport_collection.find_one({"user_id": sender_id})
            if not existing and await _claim_sequence(checkin["_id"], "passport_reminder", sender_id):
                guest_display = checkin.get("guest_name") or f"Guest ...{sender_id[-4:]}"
                await enqueue_whatsapp_template(
                    sender_id, "passport_reminder_seq", [guest_display]
                )
        elif elapsed_scan > timedelta(hours=72):
            await _claim_sequence(checkin["_id"], "passport_reminder")  # stale — skip

    # 2. Day-1 welcome — ~24h after check-in, within a 24h window (24h–48h).
    if "day_1_welcome" not in sent:
        elapsed = now - effective_checkin
        if timedelta(hours=24) < elapsed <= timedelta(hours=48):
            if await _claim_sequence(checkin["_id"], "day_1_welcome", sender_id):
                guest_display = checkin.get("guest_name") or f"Guest ...{sender_id[-4:]}"
                await enqueue_whatsapp_template(
                    sender_id, "day_1_welcome_seq", [guest_display]
                )
        elif elapsed > timedelta(hours=48):
            await _claim_sequence(checkin["_id"], "day_1_welcome")  # stale — skip

    # 3. Mid-stay concierge — ~48h after check-in, within a 48h window (48h–96h).
    if "mid_stay_concierge" not in sent:
        elapsed = now - effective_checkin
        if timedelta(hours=48) < elapsed <= timedelta(hours=96):
            if await _claim_sequence(checkin["_id"], "mid_stay_concierge", sender_id):
                guest_display = checkin.get("guest_name") or f"Guest ...{sender_id[-4:]}"
                await enqueue_whatsapp_template(
                    sender_id, "mid_stay_concierge_seq", [guest_display]
                )
        elif elapsed > timedelta(hours=96):
            await _claim_sequence(checkin["_id"], "mid_stay_concierge")  # stale — skip

    # ── Resolve checkout date ─────────────────────────────────────────────────
    # Priority: 1) guest-provided via onboarding, 2) paid order, 3) estimate
    guest_checkout = checkin.get("checkout_date")
    latest_order = await order_collection.find_one(
        {"sender_id": sender_id, "fsm_payment_status": PaymentStatus.PAID},
        sort=[("created_at", -1)]
    )
    order_checkout = latest_order.get("check_out_date") if latest_order else None
    estimated_checkout = guest_checkout or order_checkout or (effective_checkin + timedelta(days=5))

    if not checkin.get("estimated_checkout"):
        await checkin_collection.update_one(
            {"_id": checkin["_id"]},
            {"$set": {"estimated_checkout": estimated_checkout}}
        )

    # 4. Pre-checkout reminder — 24 hours before estimated checkout
    if "pre_checkout_remind" not in sent:
        time_left = estimated_checkout - now
        if timedelta(hours=0) < time_left < timedelta(hours=24):
            if await _claim_sequence(checkin["_id"], "pre_checkout_remind", sender_id):
                guest_display = checkin.get("guest_name") or f"Guest ...{sender_id[-4:]}"
                await enqueue_whatsapp_template(
                    sender_id, "pre_checkout_remind_seq", [guest_display]
                )

    # 4b. Key return reminder — sent 2 hours before checkout
    if "key_return_remind" not in sent:
        time_left = estimated_checkout - now
        if timedelta(hours=0) < time_left < timedelta(hours=2):
            if await _claim_sequence(checkin["_id"], "key_return_remind", sender_id):
                await enqueue_whatsapp_template(
                    sender_id, "key_return_remind_seq", []
                )

    # 5. Checkout summary — 1–4 hours after estimated checkout
    if "checkout_summary" not in sent:
        hours_since = (now - estimated_checkout).total_seconds() / 3600
        if 1 <= hours_since < 4:
            if await _claim_sequence(checkin["_id"], "checkout_summary", sender_id):
                paid_orders = await order_collection.find(
                    {"sender_id": sender_id, "villa_code": villa_code, "fsm_payment_status": PaymentStatus.PAID}
                ).to_list(20)

                if paid_orders:
                    service_names = ", ".join(
                        o.get('service_name', 'Service') for o in paid_orders
                    )
                    total = sum(o.get("payment", {}).get("paid_amount", 0) for o in paid_orders)
                    await enqueue_whatsapp_template(
                        sender_id, "checkout_summary_seq",
                        [villa_code, service_names, f"IDR {total:,.0f}"]
                    )

    # 6. Feedback request — 4–24 hours after estimated checkout
    if "feedback_request" not in sent:
        hours_since = (now - estimated_checkout).total_seconds() / 3600
        if 4 <= hours_since < 24:
            if await _claim_sequence(checkin["_id"], "feedback_request", sender_id):
                guest_display = checkin.get("guest_name") or f"Guest ...{sender_id[-4:]}"
                await enqueue_whatsapp_template(
                    sender_id, "feedback_request_seq", [guest_display]
                )
                await checkin_collection.update_one(
                    {"_id": checkin["_id"]},
                    {"$set": {"status": "completed"}}
                )

    # 7. Close out finished / ancient check-ins so the butler stops re-scanning
    #    them every hour. Only close on a REAL checkout that has passed by >24h
    #    (never on the 5-day default, which may belong to a guest still staying),
    #    or when the record is older than 30 days (safety cleanup for backdated /
    #    abandoned records).
    real_checkout = bool(guest_checkout or order_checkout)
    if ((real_checkout and (now - estimated_checkout) > timedelta(hours=24))
            or (now - checkin_time) > timedelta(days=30)):
        await checkin_collection.update_one(
            {"_id": checkin["_id"], "status": "active"},
            {"$set": {"status": "completed"}},
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def mark_checkin_sent(checkin_id, trigger_name: str, guest_id: str = None):
    now = datetime.now()
    await checkin_collection.update_one(
        {"_id": checkin_id},
        {"$addToSet": {"sent_automations": trigger_name}}
    )
    if guest_id:
        await automation_log_col.insert_one({
            "guest_id": guest_id,
            "event_type": trigger_name,
            "sent_at": now,
            "checkin_id": str(checkin_id)
        })


async def check_order_triggers(order: dict, now: datetime):
    # Kept for future non-QR service-specific flows.
    pass


# ── Phase 3A: Auto-Completion ────────────────────────────────────────────────

def get_service_end_datetime(order_date: datetime, time_str: str) -> datetime | None:
    if not order_date or not time_str:
        return None
    
    if not isinstance(order_date, datetime):
        return None

    time_str = time_str.strip().upper()
    parts = re.split(r'\s*[-–—to]+\s*', time_str)
    
    try:
        if len(parts) >= 2:
            start_part = parts[0].strip()
            end_part = parts[-1].strip()
            
            end_time_dt = date_parser.parse(end_part, default=order_date)
            start_time_dt = date_parser.parse(start_part, default=order_date)
            
            if end_time_dt < start_time_dt:
                end_time_dt += timedelta(days=1)
                
            return end_time_dt
        else:
            return date_parser.parse(time_str, default=order_date)
    except Exception as e:
        logger.error(f"Error parsing service time for order: {e}")
        return None


async def check_order_auto_completion(now: datetime):
    """
    Scan for CONFIRMED orders that are 24h past service end and mark as COMPLETED.
    """
    logger.info("🤖 Butler: Running order auto-completion sweep...")
    
    query = {
        "booking_status": BookingStatus.CONFIRMED,
        "is_locked": {"$ne": True}
    }
    
    completed_count = 0
    async for order in order_collection.find(query):
        order_num = order.get("order_number")
        order_date = order.get("date")
        time_str = order.get("time")
        
        if not order_date or not time_str:
            logger.info(f"Butler: Order {order_num} missing date/time, skipping auto-completion.")
            continue
            
        end_dt = get_service_end_datetime(order_date, time_str)
        if not end_dt:
            logger.info(f"Butler: Order {order_num} has no time window range '{time_str}', skipping auto-completion.")
            continue
            
        # PD-01: end_dt is Bali local time (naive) — subtract UTC+8 offset
        # so auto-completion fires at service_end+24h in Bali time, not 8h late.
        _BALI_OFFSET = timedelta(hours=8)
        completion_threshold = end_dt - _BALI_OFFSET + timedelta(hours=24)
        
        if now >= completion_threshold:
            try:
                from app.services.status_service import BookingStatusManager
                res = await BookingStatusManager.transition_booking_status(
                    order_number=order_num,
                    target_status=BookingStatus.COMPLETED,
                    changed_by="SYSTEM_AUTO_COMPLETE",
                    reason=f"Auto-completed 24h after service end ({end_dt.strftime('%Y-%m-%d %H:%M')})"
                )
                if res["success"]:
                    completed_count += 1
                else:
                    logger.error(f"Butler: Failed auto-completion for {order_num}: {res.get('error')}")
            except Exception as e:
                logger.error(f"Butler: Error auto-completing {order_num}: {e}")

    if completed_count > 0:
        logger.warning(f"🤖 Butler: Auto-completed {completed_count} order(s).")

async def check_scheduled_disbursements(now: datetime):
    """Phase 3C-1: Scan for SCHEDULED disbursements that are due for processing."""
    from app.services.status_service import BookingStatusManager
    query = {
        "disbursement_status": DisbursementStatus.SCHEDULED,
        "disbursement_due_at": {"$lte": now},
        "booking_status":      BookingStatus.COMPLETED,
        "fsm_payment_status":  PaymentStatus.PAID,
        "is_locked":           {"$ne": True}
    }
    
    cursor = order_collection.find(query)
    async for order in cursor:
        order_number = order.get("order_number")
        logger.info(f"Automation Butler: Processing disbursement for {order_number}")
        
        # Phase 3C-2A: Generate stable idempotency keys if they don't exist
        sp_ref = order.get("sp_disbursement_reference") or f"{order_number}-SP"
        villa_ref = order.get("villa_disbursement_reference") or f"{order_number}-VL"
        
        # Phase 3C-2B: Build / Simulate payloads
        from app.services.disbursement_builder import build_disbursement_payloads
        simulation = await build_disbursement_payloads(order)
        
        await BookingStatusManager.transition_disbursement_status(
            order_number=order_number,
            target_status=DisbursementStatus.PENDING,
            changed_by="SYSTEM_DISBURSEMENT_WORKER",
            reason="Scheduled disbursement window reached. Moving to PENDING for execution.",
            extra_fields={
                "sp_disbursement_reference": sp_ref,
                "villa_disbursement_reference": villa_ref,
                "disbursement_attempt_count": 0,
                "disbursement_simulation": simulation
            }
        )

    # --- SWEEP 2: PENDING -> DISTRIBUTED (Execution) ---
    pending_query = {
        "disbursement_status": DisbursementStatus.PENDING,
        "booking_status":      BookingStatus.COMPLETED,
        "fsm_payment_status":  PaymentStatus.PAID,
        "is_locked":           {"$ne": True},
        "disbursement_simulation.overall_ready": True
    }
    
    from app.services.disbursement_executor import execute_payouts
    
    pending_cursor = order_collection.find(pending_query)
    async for order in pending_cursor:
        order_num = order.get("order_number")
        logger.info(f"Automation Butler: Initiating execution for PENDING order {order_num}")
        try:
            # execute_payouts handles internal status transitions (DISTRIBUTED or FAILED)
            await execute_payouts(order)
        except Exception as e:
            logger.error(f"Automation Butler: Critical error executing payouts for {order_num}: {e}")
