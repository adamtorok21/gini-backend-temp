import datetime
import logging
from dateutil import parser
from app.db.session import guest_profile_collection
from app.models.guest_profile import GuestProfile

logger = logging.getLogger(__name__)

def get_stay_status(check_in, check_out):
    """
    Determine if the guest stay is in the future, currently active, or in the past.
    Used for tailoring AI responses and dashboard filtering.
    """
    if not check_in or not check_out:
        return "unknown"
    
    try:
        now = datetime.datetime.now().date()
        
        # Date parsing with fallback
        if isinstance(check_in, str):
            c_in = parser.parse(check_in).date()
        elif isinstance(check_in, datetime.datetime):
            c_in = check_in.date()
        else:
            c_in = check_in
            
        if isinstance(check_out, str):
            c_out = parser.parse(check_out).date()
        elif isinstance(check_out, datetime.datetime):
            c_out = check_out.date()
        else:
            c_out = check_out
            
        if now < c_in:
            return "pre-arrival"
        elif c_in <= now <= c_out:
            return "active"
        else:
            return "post-stay"
    except Exception as e:
        logger.error(f"Error calculating stay status: {e}")
        return "unknown"

async def get_guest_context_by_phone(phone_number: str):
    """
    Retrieves the full guest context including identity and stay timing.
    """
    if not phone_number:
        return None
        
    profile = await guest_profile_collection.find_one({"phone_number": phone_number})
    if not profile:
        return None
        
    # Inject dynamic stay status
    profile["stay_status"] = get_stay_status(
        profile.get("check_in_date"), 
        profile.get("check_out_date")
    )
    return profile
