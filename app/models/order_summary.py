# RECONSTRUCTED model module (sandbox) — original app/models/ was missing from handover.
# Enum members inferred from usage across the codebase; string values are best-effort.
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict


class BookingStatus(str, Enum):
    AWAITING_SP_CONFIRMATION = "AWAITING_SP_CONFIRMATION"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    CONFIRMED = "CONFIRMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"


class PaymentStatus(str, Enum):
    UNPAID = "UNPAID"
    PAYMENT_LINK_SENT = "PAYMENT_LINK_SENT"
    PAID = "PAID"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class DisbursementStatus(str, Enum):
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    HELD = "HELD"
    DISTRIBUTED = "DISTRIBUTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class PayoutStatus(str, Enum):
    PENDING = "PENDING"
    DISBURSED = "DISBURSED"
    FAILED = "FAILED"
    ALL_BLOCKED = "ALL_BLOCKED"
    BLOCKED_DISTRIBUTION_DATA = "BLOCKED_DISTRIBUTION_DATA"
    BLOCKED_MISSING_BANK = "BLOCKED_MISSING_BANK"
    BLOCKED_MISSING_CODE = "BLOCKED_MISSING_CODE"
    BLOCKED_MISSING_VILLA_CODE = "BLOCKED_MISSING_VILLA_CODE"
    BLOCKED_RECONCILIATION = "BLOCKED_RECONCILIATION"


class SpPayoutStatus(str, Enum):
    PENDING = "PENDING"
    DISBURSED = "DISBURSED"
    FAILED = "FAILED"
    ALL_BLOCKED = "ALL_BLOCKED"
    BLOCKED_MISSING_BANK = "BLOCKED_MISSING_BANK"
    BLOCKED_MISSING_CODE = "BLOCKED_MISSING_CODE"


class Order(BaseModel):
    """Booking/order model. extra='allow' so unmapped fields still round-trip."""
    model_config = ConfigDict(extra="allow")

    order_number: Optional[str] = None
    sender_id: Optional[str] = None
    service_name: Optional[str] = None
    service_provider_code: Optional[str] = None
    villa_code: Optional[str] = None
    villa_name: Optional[str] = None
    price: Optional[Any] = None
    promo_code: Optional[str] = None
    date: Optional[str] = None
    guest_name: Optional[str] = None
    phone_number: Optional[str] = None
    booking_status: Optional[str] = None
    payment_status: Optional[str] = None

    def get(self, key, default=None):
        return getattr(self, key, default)
