# RECONSTRUCTED model module (sandbox) — original was missing from handover.
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict


class GuestProfile(BaseModel):
    model_config = ConfigDict(extra="allow")

    guest_id: Optional[str] = None
    name: Optional[str] = None
    guest_name: Optional[str] = None
    phone_number: Optional[str] = None
    sender_id: Optional[str] = None
    villa_code: Optional[str] = None
    villa_name: Optional[str] = None
    location: Optional[str] = None
    check_in: Optional[Any] = None
    check_out: Optional[Any] = None

    def get(self, key, default=None):
        return getattr(self, key, default)
