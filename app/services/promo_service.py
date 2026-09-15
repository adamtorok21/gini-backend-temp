from app.db.session import db
from datetime import datetime
from typing import Optional, Tuple
import logging

logger = logging.getLogger(__name__)
promo_collection = db["promo_codes"]

async def validate_promo_code(code: str, base_amount: float) -> Tuple[bool, float, str]:
    """
    Validates a code and returns (is_valid, final_amount, message).
    """
    promo = await promo_collection.find_one({"code": code.upper(), "active": True})
    
    if not promo:
        return False, base_amount, "Invalid promo code"
    
    # Check expiry
    if promo.get("expiry") and promo["expiry"] < datetime.utcnow():
        return False, base_amount, "Promo code expired"
    
    # Check usage limits
    if promo.get("usage_limit") and promo.get("current_usage", 0) >= promo["usage_limit"]:
        return False, base_amount, "Promo code usage limit reached"
        
    discount = 0.0
    if promo["type"] == "percentage":
        discount = base_amount * (promo["value"] / 100)
    else: # fixed amount
        discount = promo["value"]
        
    final_amount = max(0, base_amount - discount)
    
    return True, final_amount, f"Applied {promo['value']}% discount" if promo["type"] == "percentage" else f"Applied IDR {promo['value']} discount"

async def increment_promo_usage(code: str):
    await promo_collection.update_one(
        {"code": code.upper()},
        {"$inc": {"current_usage": 1}}
    )
