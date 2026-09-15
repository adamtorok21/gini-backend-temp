import logging
import re
from datetime import datetime, UTC, timedelta
from typing import Optional, Dict, Any, List, Set
from pymongo import ReturnDocument
from dateutil import parser as date_parser

from app.db.session import order_collection
from app.models.order_summary import BookingStatus, PaymentStatus, DisbursementStatus

logger = logging.getLogger(__name__)


def _to_amount(value: Any) -> int:
    """Parse a price/amount that may be an int, float, or formatted string
    (e.g. '10,000', 'Rp 10.000', '10000.0') into an int number of rupiah.

    Google Sheets prices are comma-formatted strings ('10,000'). Bare
    int('10,000') raises ValueError — which previously crashed the CONFIRMED
    guard and aborted the entire PAID webhook, silently ghosting paid guests.
    Returns 0 when no digits are present.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else 0


def _resolve_disbursement_due_at(order: Dict[str, Any], now: datetime) -> datetime:
    """
    TD-1: Calculate disbursement_due_at as service_end_datetime + 24 hours.

    Service end time is derived from:
      1. order["date"] (datetime) + order["time"] (string, may be a range "14:00-16:00")
      2. Fallback: now + 24h (payment time + 24h)

    If order["time"] is a range, the END of the range is used (per spec).
    The returned datetime is timezone-naive UTC, consistent with the rest of the system.
    """
    try:
        order_date = order.get("date")

        # Try to coerce booking_date string if date is absent or not a datetime
        if not isinstance(order_date, datetime):
            booking_date_str = order.get("booking_date")
            if booking_date_str:
                try:
                    order_date = date_parser.parse(str(booking_date_str))
                except Exception:
                    order_date = None

        time_str = order.get("time", "")
        if order_date and time_str:
            time_str = time_str.strip().upper()
            parts = re.split(r'\s*[-–—to]+\s*', time_str)
            try:
                if len(parts) >= 2:
                    end_part = parts[-1].strip()
                    start_part = parts[0].strip()
                    end_dt = date_parser.parse(end_part, default=order_date)
                    start_dt = date_parser.parse(start_part, default=order_date)
                    if end_dt < start_dt:
                        end_dt += timedelta(days=1)
                else:
                    end_dt = date_parser.parse(time_str, default=order_date)

                # PD-01: end_dt is Bali local time (WITA = UTC+8, naive).
                # Subtract the offset so the stored UTC value is correct;
                # without this, disbursement_due_at is 8h too late and
                # fmtBaliDateTime shows the next calendar day.
                _BALI_OFFSET = timedelta(hours=8)
                return end_dt - _BALI_OFFSET + timedelta(hours=24)
            except Exception as parse_err:
                logger.warning(f"TD-1: Could not parse service time '{time_str}' for order {order.get('order_number')}: {parse_err}")
    except Exception as outer_err:
        logger.warning(f"TD-1: disbursement_due_at calculation failed for {order.get('order_number')}: {outer_err}")

    # Fallback: 24h from payment time
    return now + timedelta(hours=24)

class BookingStatusManager:
    """
    Central authority for all booking-related state transitions.
    Enforces FSM rules, handles locking, and maintains audit history.
    """

    # ── Legacy Status Normalization ──────────────────────────────────────────
    # Orders created before the FSM was introduced carry old string values.
    # Map them to canonical FSM values before any transition lookup.

    _LEGACY_BOOKING_STATUS_MAP: Dict = {
        None:                       BookingStatus.AWAITING_SP_CONFIRMATION,
        "pending":                  BookingStatus.AWAITING_SP_CONFIRMATION,
        "payment_pending":          BookingStatus.AWAITING_PAYMENT,
        "provider_confirmed":       BookingStatus.AWAITING_PAYMENT,
        "PAID":                     BookingStatus.CONFIRMED,
        "funds_distributed":        BookingStatus.CONFIRMED,
        "disbursement_initiated":   BookingStatus.CONFIRMED,
        "completed":                BookingStatus.COMPLETED,
        "cancelled":                BookingStatus.CANCELLED,
        "REFUNDED":                 BookingStatus.REFUNDED,
        "failed":                   BookingStatus.FAILED,
    }

    _LEGACY_PAYMENT_STATUS_MAP: Dict = {
        None:                PaymentStatus.UNPAID,
        "unpaid":            PaymentStatus.UNPAID,
        "payment_link_sent": PaymentStatus.PAYMENT_LINK_SENT,
        "paid":              PaymentStatus.PAID,
        "expired":           PaymentStatus.EXPIRED,
        "failed":            PaymentStatus.FAILED,
        "refunded":          PaymentStatus.REFUNDED,
    }

    @classmethod
    def _normalize(cls, status: Optional[str], field: str) -> Optional[str]:
        """Map legacy/None status values to their canonical FSM equivalents."""
        if field == "booking_status":
            return cls._LEGACY_BOOKING_STATUS_MAP.get(status, status)
        if field == "fsm_payment_status":
            return cls._LEGACY_PAYMENT_STATUS_MAP.get(status, status)
        # disbursement_status: None is already a key in _DISBURSEMENT_TRANSITIONS
        return status

    # ── Transition Maps ──────────────────────────────────────────────────────

    _BOOKING_TRANSITIONS: Dict[str, Set[str]] = {
        BookingStatus.AWAITING_SP_CONFIRMATION: {
            BookingStatus.AWAITING_PAYMENT, 
            BookingStatus.CANCELLED, 
            BookingStatus.FAILED
        },
        BookingStatus.AWAITING_PAYMENT: {
            BookingStatus.CONFIRMED, 
            BookingStatus.CANCELLED, 
            BookingStatus.FAILED
        },
        BookingStatus.CONFIRMED: {
            BookingStatus.COMPLETED, 
            BookingStatus.CANCELLED, 
            BookingStatus.REFUNDED
        },
        BookingStatus.COMPLETED: {
            BookingStatus.REFUNDED, 
            BookingStatus.CANCELLED
        },
        BookingStatus.CANCELLED: set(),
        BookingStatus.REFUNDED:  set(),
        BookingStatus.FAILED:    {BookingStatus.AWAITING_SP_CONFIRMATION, BookingStatus.CANCELLED},
    }

    _PAYMENT_TRANSITIONS: Dict[str, Set[str]] = {
        PaymentStatus.UNPAID: {
            PaymentStatus.PAYMENT_LINK_SENT, 
            PaymentStatus.PAID, 
            PaymentStatus.FAILED
        },
        PaymentStatus.PAYMENT_LINK_SENT: {
            PaymentStatus.PAID, 
            PaymentStatus.EXPIRED, 
            PaymentStatus.FAILED
        },
        PaymentStatus.PAID: {
            PaymentStatus.REFUNDED
        },
        PaymentStatus.EXPIRED:  {PaymentStatus.PAYMENT_LINK_SENT}, # retry
        PaymentStatus.FAILED:   {PaymentStatus.PAYMENT_LINK_SENT}, # retry
        PaymentStatus.REFUNDED: set(),
    }

    _DISBURSEMENT_TRANSITIONS: Dict[str, Set[str]] = {
        # None covers legacy orders created before disbursement_status field was added
        None: {
            DisbursementStatus.NOT_ELIGIBLE,
            DisbursementStatus.SCHEDULED,
        },
        DisbursementStatus.NOT_ELIGIBLE: {
            DisbursementStatus.SCHEDULED,
            DisbursementStatus.CANCELLED
        },
        DisbursementStatus.SCHEDULED: {
            DisbursementStatus.PENDING, 
            DisbursementStatus.HELD, 
            DisbursementStatus.CANCELLED
        },
        DisbursementStatus.HELD: {
            DisbursementStatus.PENDING,
            # Resume (HELD -> SCHEDULED): re-arm the payout on its ORIGINAL due
            # date. The SCHEDULED transition re-resolves disbursement_due_at
            # (service_end + 24h) so the due date/time reappears and the existing
            # SCHEDULED -> PENDING -> DISTRIBUTED butler pipeline executes the
            # payout (building the simulation). Additive: does not remove any
            # existing transition or touch the payout/execution code.
            DisbursementStatus.SCHEDULED,
            DisbursementStatus.CANCELLED
        },
        DisbursementStatus.PENDING: {
            DisbursementStatus.DISTRIBUTED,
            DisbursementStatus.FAILED,
            DisbursementStatus.HELD,
            DisbursementStatus.CANCELLED
        },
        DisbursementStatus.DISTRIBUTED: set(),
        DisbursementStatus.CANCELLED:   set(),
        DisbursementStatus.FAILED:      {DisbursementStatus.PENDING, DisbursementStatus.CANCELLED},
    }

    @classmethod
    def get_allowed_transitions(cls, current_status: str, field: str = "booking_status") -> List[str]:
        """Returns the list of allowed target statuses for the current status and field."""
        transition_map = {
            "booking_status": cls._BOOKING_TRANSITIONS,
            "fsm_payment_status": cls._PAYMENT_TRANSITIONS,
            "disbursement_status": cls._DISBURSEMENT_TRANSITIONS
        }.get(field)

        if not transition_map:
            return []

        normalized = cls._normalize(current_status, field)
        return list(transition_map.get(normalized, set()))

    @classmethod
    def get_blocked_transitions(cls, current_status: str, field: str = "booking_status") -> List[Dict[str, str]]:
        """Returns all potential statuses that are blocked from the current state."""
        from app.models.order_summary import BookingStatus, PaymentStatus, DisbursementStatus

        all_statuses = {
            "booking_status": [getattr(BookingStatus, a) for a in dir(BookingStatus) if not a.startswith('__')],
            "fsm_payment_status": [getattr(PaymentStatus, a) for a in dir(PaymentStatus) if not a.startswith('__')],
            "disbursement_status": [getattr(DisbursementStatus, a) for a in dir(DisbursementStatus) if not a.startswith('__')]
        }.get(field, [])

        normalized_current = cls._normalize(current_status, field)
        allowed = set(cls.get_allowed_transitions(current_status, field))
        blocked = []

        for status in all_statuses:
            if status != normalized_current and status not in allowed:
                reason = f"FSM constraint: {normalized_current} -> {status} is not allowed."
                blocked.append({"status": status, "reason": reason})

        return blocked

    # ── Public Methods ───────────────────────────────────────────────────────

    @classmethod
    async def transition_booking_status(
        cls, 
        order_number: str, 
        target_status: str, 
        changed_by: str, 
        reason: Optional[str] = None,
        bypass_lock: bool = False
    ) -> Dict[str, Any]:
        """Atomically transition booking status with validation and audit log."""
        res = await cls._perform_transition(
            order_number, "booking_status", target_status, changed_by, reason, cls._BOOKING_TRANSITIONS, bypass_lock
        )

        # Side-effect: Phase 3B - Trigger disbursement eligibility when completed
        if res["success"] and target_status == BookingStatus.COMPLETED:
            order = res.get("order")
            if order:
                await cls._schedule_disbursement_if_eligible(order, changed_by)

        return res

    @classmethod
    async def transition_payment_status(
        cls, 
        order_number: str, 
        target_status: str, 
        changed_by: str, 
        reason: Optional[str] = None,
        bypass_lock: bool = False,
        extra_fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Atomically transition payment status with validation and audit log."""
        return await cls._perform_transition(
            order_number, "fsm_payment_status", target_status, changed_by, reason, cls._PAYMENT_TRANSITIONS, bypass_lock, extra_fields
        )

    @classmethod
    async def transition_disbursement_status(
        cls, 
        order_number: str, 
        target_status: str, 
        changed_by: str, 
        reason: Optional[str] = None,
        bypass_lock: bool = False,
        extra_fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Atomically transition disbursement status with validation and audit log."""
        return await cls._perform_transition(
            order_number, "disbursement_status", target_status, changed_by, reason, cls._DISBURSEMENT_TRANSITIONS, bypass_lock, extra_fields
        )


    # ── Internal Engine ──────────────────────────────────────────────────────

    @classmethod
    async def _perform_transition(
        cls,
        order_number: str,
        field_name: str,
        target_status: str,
        changed_by: str,
        reason: Optional[str],
        transition_map: Dict[str, Set[str]],
        bypass_lock: bool = False,
        extra_fields: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Generic transition engine for any FSM field."""
        
        # 1. Fetch current state
        order = await order_collection.find_one({"order_number": order_number})
        if not order:
            return {"success": False, "error": f"Order {order_number} not found"}

        current_status = order.get(field_name)
        is_locked = order.get("is_locked", False)

        # 2. Lock check
        if is_locked and not bypass_lock:
            return {"success": False, "error": f"Order {order_number} is locked and cannot be modified"}

        # 3. Idempotency check
        if current_status == target_status:
            return {"success": True, "message": f"Already in status {target_status}", "no_change": True}

        # 4. Validation — normalize legacy status values before FSM lookup.
        # Legacy orders never had booking_status written; their true state lives
        # in the legacy 'status' field (e.g. funds_distributed = paid/confirmed).
        # Without this fallback a legacy paid order normalizes to
        # AWAITING_SP_CONFIRMATION and can never be refunded or completed.
        _lookup_status = current_status
        if field_name == "booking_status" and current_status is None:
            _lookup_status = order.get("status")
        normalized_current = cls._normalize(_lookup_status, field_name)
        allowed = transition_map.get(normalized_current, set())
        if target_status not in allowed:
            # Special case: Admin override can force certain transitions
            if changed_by != "SYSTEM_ADMIN":
                return {
                    "success": False,
                    "error": f"Invalid transition: {current_status} -> {target_status}"
                }

        # 5. Guard Conditions (Phase 2B)
        # Defense in depth: a guard exception must degrade to a rejected
        # transition (logged), never propagate and abort the caller. An
        # unhandled guard exception here previously crashed the entire PAID
        # webhook and silently ghosted paid guests (comma-price ValueError).
        try:
            guard_error = cls._validate_target_status(order, field_name, target_status)
        except Exception as guard_exc:
            logger.error(
                f"FSM guard raised for {order_number} {field_name} -> {target_status}: {guard_exc}"
            )
            return {"success": False, "error": f"Guard evaluation error: {guard_exc}"}
        if guard_error:
            return {"success": False, "error": guard_error}

        # 6. Atomic Update
        now = datetime.now(UTC)
        history_entry = {
            "field":      field_name,
            "from":       current_status,
            "to":         target_status,
            "changed_by": changed_by,
            "timestamp":  now,
            "reason":     reason
        }

        # Lock terminal states automatically
        auto_lock = target_status in (BookingStatus.CANCELLED, BookingStatus.REFUNDED, PaymentStatus.REFUNDED)

        # Legacy mapping for display (backward compatibility)
        legacy_status_map = {
            BookingStatus.AWAITING_SP_CONFIRMATION: "pending",
            BookingStatus.AWAITING_PAYMENT:         "payment_pending",
            BookingStatus.CONFIRMED:                "PAID",
            BookingStatus.COMPLETED:                "completed",
            BookingStatus.CANCELLED:                "cancelled",
            BookingStatus.REFUNDED:                 "REFUNDED",
            BookingStatus.FAILED:                   "failed",
        }

        update_query = {
            "$set": {
                field_name:   target_status,
                "updated_at": now,
            },
            "$push": {
                "status_history": history_entry
            }
        }

        # Inject extra fields if provided
        if extra_fields:
            for k, v in extra_fields.items():
                update_query["$set"][k] = v

        # Sync legacy 'status' field if we are updating booking_status
        if field_name == "booking_status" and target_status in legacy_status_map:
            update_query["$set"]["status"] = legacy_status_map[target_status]

        # TD-1 / HR-01: Set disbursement_due_at for SCHEDULED transitions.
        # Smart timing: if the natural due time (service_end + 24h) is still in the
        # future, keep it (preserves original schedule on Resume-before-due).
        # If it has already passed (Resume-after-due), schedule 1h from now so the
        # butler picks it up quickly without an indefinite wait.
        #
        # CRITICAL: _resolve_disbursement_due_at returns a timezone-naive UTC datetime
        # (Motor returns naive datetimes). datetime.now(UTC) is timezone-aware.
        # Comparing naive > aware raises TypeError in Python 3. Use _now_naive
        # (stripped of tzinfo) for the calculation and comparison so both sides
        # are consistently naive UTC.  The stored value is intentionally naive —
        # the butler also uses datetime.now() (naive) for its $lte comparison.
        if field_name == "disbursement_status" and target_status == DisbursementStatus.SCHEDULED:
            try:
                _now_naive = now.replace(tzinfo=None)
                natural_due = _resolve_disbursement_due_at(order, _now_naive)
                due_at = natural_due if natural_due > _now_naive else _now_naive + timedelta(hours=1)
                update_query["$set"]["disbursement_due_at"] = due_at
            except Exception as _due_err:
                logger.warning(
                    f"HR-01: disbursement_due_at calculation failed for {order_number}: {_due_err}. "
                    "Falling back to now+1h so the butler picks it up quickly."
                )
                update_query["$set"]["disbursement_due_at"] = now.replace(tzinfo=None) + timedelta(hours=1)

        if auto_lock:
            update_query["$set"]["is_locked"] = True

        try:
            updated_doc = await order_collection.find_one_and_update(
                {"order_number": order_number, field_name: current_status}, # Atomic guard
                update_query,
                return_document=ReturnDocument.AFTER
            )

            if not updated_doc:
                return {"success": False, "error": "Race condition detected: status changed during processing"}

            logger.info(f"FSM Transition: {order_number} {field_name} {current_status} -> {target_status} by {changed_by}")
            return {"success": True, "new_status": target_status, "order": updated_doc}

        except Exception as e:
            logger.error(f"FSM Error for {order_number}: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    async def _schedule_disbursement_if_eligible(cls, order: Dict[str, Any], changed_by: str):
        """Phase 3B: Side-effect logic to schedule disbursement when booking is completed."""
        order_number = order.get("order_number")
        current_disbursement_status = order.get("disbursement_status")
        payment_status = order.get("fsm_payment_status")
        
        # Rule: Only transition if not already in a restricted state
        restricted_states = {
            DisbursementStatus.HELD, 
            DisbursementStatus.DISTRIBUTED, 
            DisbursementStatus.CANCELLED, 
            DisbursementStatus.FAILED
        }
        
        if current_disbursement_status in restricted_states:
            logger.info(f"FSM: Skipping disbursement scheduling for {order_number} (Status: {current_disbursement_status})")
            return

        # Phase 3B Improvement: Do not schedule if not PAID
        if payment_status != PaymentStatus.PAID:
            logger.info(f"FSM: Skipping disbursement scheduling for {order_number} (Payment: {payment_status})")
            return
            
        # Trigger transition to SCHEDULED
        await cls.transition_disbursement_status(
            order_number=order_number,
            target_status=DisbursementStatus.SCHEDULED,
            changed_by=changed_by,
            reason="Automatically scheduled upon booking completion."
        )

    @classmethod
    def _validate_target_status(cls, order: Dict[str, Any], field: str, target: str) -> Optional[str]:
        """Phase 2B: Enforce complex business rules for specific transitions."""
        
        # 1. Booking Confirmation Guards
        if field == "booking_status" and target == BookingStatus.CONFIRMED:
            payment_status = order.get("fsm_payment_status")
            sp_confirmed = order.get("confirmed_by_provider")
            
            if payment_status != PaymentStatus.PAID:
                return "Cannot confirm booking: payment status is not PAID"
            
            if not sp_confirmed:
                return "Cannot confirm booking: no service provider has confirmed/accepted yet"
            
            # Amount matching check — prices may be comma-formatted strings
            # ('10,000') from Google Sheets, so parse defensively. Bare int()
            # on a comma string raises ValueError.
            expected_price = order.get("price", 0)
            paid_amount = order.get("payment", {}).get("paid_amount", 0)
            if _to_amount(paid_amount) < _to_amount(expected_price):
                return f"Cannot confirm booking: paid amount ({paid_amount}) is less than expected ({expected_price})"

        # 2. Prevent reopening terminal bookings via webhook
        if field == "booking_status" and target in (BookingStatus.AWAITING_PAYMENT, BookingStatus.CONFIRMED):
            current = order.get("booking_status")
            if current in (BookingStatus.CANCELLED, BookingStatus.REFUNDED, BookingStatus.FAILED):
                return f"Cannot transition {current} booking back to {target}"

        return None

    @classmethod
    async def process_manual_refund(
        cls, 
        order_number: str, 
        changed_by: str, 
        reason: Optional[str] = "Admin Refund"
    ) -> Dict[str, Any]:
        """
        Perform a manual refund: sets booking and payment status to REFUNDED,
        cancels disbursement, and locks the order.
        """
        order = await order_collection.find_one({"order_number": order_number})
        if not order:
            return {"success": False, "error": f"Order {order_number} not found"}

        # ── Guardrails ───────────────────────────────────────────────────────
        # Refund requires a paid order in CONFIRMED/COMPLETED. Legacy (pre-FSM)
        # orders carry their paid state in the legacy 'status' field with
        # booking_status/fsm_payment_status unset — normalize before checking.
        raw_payment = order.get("fsm_payment_status")
        raw_booking = order.get("booking_status")
        legacy_status = order.get("status")

        payment_status = cls._normalize(raw_payment, "fsm_payment_status")
        booking_status = cls._normalize(
            raw_booking if raw_booking is not None else legacy_status, "booking_status"
        )
        legacy_paid = legacy_status in ("funds_distributed", "disbursement_initiated", "PAID")

        if payment_status != PaymentStatus.PAID and not legacy_paid:
            return {"success": False, "error": f"Cannot refund: payment status is {raw_payment}, not PAID"}

        if booking_status not in (BookingStatus.CONFIRMED, BookingStatus.COMPLETED):
            return {"success": False, "error": f"Cannot refund: booking status is {raw_booking or legacy_status}, must be CONFIRMED or COMPLETED"}

        # ── Atomically Update All Statuses ───────────────────────────────────
        # We use bypass_lock=True because we are doing multiple transitions 
        # that will eventually lock the order.
        
        # 1. Booking -> REFUNDED
        b_res = await cls.transition_booking_status(order_number, BookingStatus.REFUNDED, changed_by, reason, bypass_lock=True)
        if not b_res["success"]: return b_res

        # 2. Payment -> REFUNDED. Legacy paid orders have no fsm_payment_status;
        # backfill UNPAID -> PAID first so the REFUNDED transition is FSM-legal
        # and the backfill itself is recorded in status_history.
        if payment_status != PaymentStatus.PAID:
            p0 = await cls.transition_payment_status(
                order_number, PaymentStatus.PAID, changed_by,
                f"Backfill from legacy paid order (status={legacy_status})", bypass_lock=True
            )
            if not p0["success"]: return p0
        p_res = await cls.transition_payment_status(order_number, PaymentStatus.REFUNDED, changed_by, reason, bypass_lock=True)
        if not p_res["success"]: return p_res

        # 3. Disbursement -> CANCELLED (if not already distributed and not None)
        # Skip when None: order has no disbursement scheduled (no distribution_data),
        # so there is nothing to cancel. Attempting the transition would fail because
        # None -> CANCELLED is not in _DISBURSEMENT_TRANSITIONS and would return an
        # error that aborts this function, leaving booking+payment already REFUNDED
        # but the refund_requests doc stuck in "pending" with no recovery path.
        current_disb = order.get("disbursement_status")
        if current_disb is not None and current_disb != DisbursementStatus.DISTRIBUTED:
            d_res = await cls.transition_disbursement_status(order_number, DisbursementStatus.CANCELLED, changed_by, reason, bypass_lock=True)
            if not d_res["success"]: return d_res

        # 4. Final Lock (explicitly lock the order)
        final_order = await order_collection.find_one_and_update(
            {
                "order_number": order_number, 
                "booking_status": BookingStatus.REFUNDED # Ensure it hasn't changed again
            },
            {"$set": {"is_locked": True}},
            return_document=ReturnDocument.AFTER
        )

        return {"success": True, "order": final_order}

    @classmethod
    async def process_reassignment(
        cls,
        order_number: str,
        changed_by: str,
        reason: Optional[str] = "Admin Reassignment"
    ) -> Dict[str, Any]:
        """
        Handle the surgery of reassigning an order to a new SP.
        Resets booking status to AWAITING_SP_CONFIRMATION and clears SP fields.
        """
        order = await order_collection.find_one({"order_number": order_number})
        if not order:
            return {"success": False, "error": f"Order {order_number} not found"}
            
        if order.get("is_locked"):
            return {"success": False, "error": "Order is locked and cannot be reassigned"}
            
        current_booking = order.get("booking_status")
        # Allowed only from CONFIRMED or AWAITING_PAYMENT
        if current_booking not in (BookingStatus.CONFIRMED, BookingStatus.AWAITING_PAYMENT):
            return {"success": False, "error": f"Cannot reassign order in status {current_booking}"}
            
        # Determine target disbursement status
        current_disbursement = order.get("disbursement_status")
        
        # Disbursement Guard: Cannot reassign if already distributed
        if current_disbursement == DisbursementStatus.DISTRIBUTED:
            return {"success": False, "error": "Cannot reassign: funds already distributed to old provider"}

        target_disbursement = current_disbursement
        if current_disbursement in (DisbursementStatus.SCHEDULED, DisbursementStatus.PENDING):
            target_disbursement = DisbursementStatus.NOT_ELIGIBLE
            
        update_data = {
            "booking_status": BookingStatus.AWAITING_SP_CONFIRMATION,
            "disbursement_status": target_disbursement,
            "status": "pending", # Legacy display status
            "service_provider_code": None,
            "confirmed_by_provider": None,
            "sp_accepted_at": None,
            "sp_notified_at": None,
            "updated_at": datetime.now(UTC)
        }
        
        # Record in audit history
        audit_entry = {
            "timestamp": datetime.now(UTC),
            "changed_by": changed_by,
            "action": "REASSIGN",
            "from_booking_status": current_booking,
            "to_booking_status": BookingStatus.AWAITING_SP_CONFIRMATION,
            "from_disbursement_status": current_disbursement,
            "to_disbursement_status": target_disbursement,
            "reason": reason
        }
        
        res = await order_collection.find_one_and_update(
            {
                "order_number": order_number,
                "booking_status": current_booking, # Atomic Guard
                "is_locked": False # Double Guard
            },
            {
                "$set": update_data,
                "$push": {"status_history": audit_entry}
            },
            return_document=ReturnDocument.AFTER
        )
        
        logger.info(f"FSM: Order {order_number} reassigned by {changed_by}. Status reset to AWAITING_SP_CONFIRMATION.")
        return {"success": True, "order": res}
