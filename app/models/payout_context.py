# RECONSTRUCTED model module (sandbox) — original was missing from handover.
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict


class PayoutContextError(Exception):
    """Raised when a payout context cannot be built (missing bank, code, etc.)."""
    pass


class PayoutContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    order_number: Optional[str] = None
    service_provider_code: Optional[str] = None
    villa_code: Optional[str] = None
    sp_amount: Optional[Any] = None
    villa_amount: Optional[Any] = None
    sp_bank: Optional[Any] = None
    villa_bank: Optional[Any] = None
    summary: Optional[Any] = None

    def get(self, key, default=None):
        return getattr(self, key, default)
