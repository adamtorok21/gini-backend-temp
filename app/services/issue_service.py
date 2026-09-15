from app.db.session import db, issue_collection
from datetime import datetime
from typing import Optional, List
import logging

logger = logging.getLogger(__name__)

# issue_collection is db["issues"] from session.py — single source of truth
# Both WhatsApp and web submissions write to this collection.

async def create_issue(
    user_id: str,
    villa_code: str,
    issue_text: str,
    priority: str = "medium",
    image_url: Optional[str] = None,
    source: str = "web",
    guest_id: Optional[str] = None,
    s3_key: Optional[str] = None,
) -> dict:
    """
    Creates a new maintenance issue from a web submission and returns the saved document.
    Field names are normalized to match WhatsApp schema so the dashboard reads both uniformly.

    ISSUE-PHOTO-01 (2026-08-25): s3_key is the private-storage key (see
    upload_secure_file in bucket.py) -- the dashboard resolves it to a
    presigned URL at serve time (GET /dashboard-api/issues), same pattern
    already used for passports. image_url is kept for callers that already
    have a direct URL (e.g. the WhatsApp path in media_upload.py, which
    stores both media_url and s3_key on its own document).
    """
    issue_data = {
        "sender_id": user_id,
        "guest_id": guest_id or user_id,
        "villa_code": villa_code,
        "description": issue_text,
        "priority": priority,
        "status": "open",
        "media_url": image_url,
        "s3_key": s3_key,
        "media_type": "image" if (image_url or s3_key) else "text",
        "source": source,
        "timestamp": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "history": [
            {
                "status": "open",
                "timestamp": datetime.utcnow(),
                "note": "Issue submitted by guest"
            }
        ]
    }

    result = await issue_collection.insert_one(issue_data)
    issue_data["_id"] = str(result.inserted_id)

    # NOTIF-VM-01 (2026-08-25): the WhatsApp free-text issue-reporting path
    # already notifies the villa manager (whatsapp_func.py); this web
    # submission path never did. Non-blocking -- a notify failure must never
    # fail the guest's already-saved submission. Mirrors the pattern used by
    # POST /amenities/request (amenity_routes.py).
    #
    # ISSUE-VM-VISIBILITY-01 (2026-08-27, live report -- Clay): "I didnt
    # receive any notification as VM" for a maintenance issue. Traced this
    # exact same silent-failure branch as PASSPORT-VM-VISIBILITY-01: when the
    # villa's sheet row has no manager contact number in any checked column,
    # this was already correctly non-fatal (submission still succeeds), but
    # invisible outside a server log line. staff_notified now surfaces it on
    # the returned issue_data so the frontend can tell the guest honestly.
    staff_notified = False
    try:
        from app.utils.whatsapp_func import get_villa_whatsapp_by_code, send_whatsapp_message
        from app.services.menu_services import get_villa_info_by_code

        villa_info = await get_villa_info_by_code(villa_code)
        villa_name = (villa_info or {}).get("name") or villa_code
        villa_phone = await get_villa_whatsapp_by_code(villa_code)
        if villa_phone:
            staff_msg = (
                f"🚨 *New Maintenance Issue*\n\n"
                f"• *Villa:* {villa_name}\n"
                f"• *Issue:* {issue_text}\n"
                f"• *Priority:* {priority}\n\n"
                f"Please check the dashboard for details."
            )
            await send_whatsapp_message(villa_phone, staff_msg)
            staff_notified = True
            logger.info(f"Villa staff notified via WhatsApp: {villa_phone} for issue {issue_data['_id']}")
        else:
            logger.warning(f"No villa WhatsApp number found for {villa_code} — staff not notified")
    except Exception as notify_err:
        logger.warning(f"Villa staff notification failed (non-fatal): {notify_err}")

    issue_data["staff_notified"] = staff_notified
    return issue_data


async def get_villa_issues(villa_code: str, status: Optional[str] = None) -> List[dict]:
    query = {"villa_code": villa_code}
    if status:
        query["status"] = status

    issues = []
    async for issue in issue_collection.find(query).sort("timestamp", -1):
        issue["_id"] = str(issue["_id"])
        issues.append(issue)
    return issues
