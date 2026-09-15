import logging
import uuid
import httpx
import boto3
from botocore.config import Config
from app.settings.config import settings
from app.db.session import db, guest_profile_collection
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)
passport_collection = db["passports"]
issue_collection = db.get_collection("issues")

_s3 = boto3.client(
    's3',
    aws_access_key_id=settings.AWS_ACCESS_KEY,
    aws_secret_access_key=settings.AWS_SECRET_KEY,
    region_name=settings.AWS_REGION,
    config=Config(signature_version='s3v4')
)

async def download_whatsapp_media(media_id: str):
    """Downloads media from WhatsApp given its Media ID."""
    # Use v22.0 to match the current app configuration set in .env
    url = f"https://graph.facebook.com/v22.0/{media_id}"
    headers = {"Authorization": f"Bearer {settings.access_token}"}
    
    logger.info(f"Downloading WhatsApp media info for ID: {media_id}")
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        if res.status_code != 200:
            logger.error(f"Failed to get media URL for {media_id}: {res.text}")
            res.raise_for_status()
            
        media_url = res.json().get("url")
        if not media_url:
            logger.error(f"No media URL found in response for {media_id}: {res.json()}")
            raise ValueError("Media URL not found")
            
        logger.info(f"Fetching actual media content from {media_url[:50]}...")
        media_res = await client.get(media_url, headers=headers)
        if media_res.status_code != 200:
            logger.error(f"Failed to download media content from {media_url[:50]}: {media_res.text}")
            media_res.raise_for_status()
        
        content_type = media_res.headers.get("content-type", "image/jpeg")
        logger.info(f"Successfully downloaded {len(media_res.content)} bytes of type {content_type}")
        return media_res.content, content_type

def upload_bytes_to_s3(file_bytes: bytes, content_type: str, folder: str = "passports") -> tuple:
    """Uploads file bytes directly to S3 without ACLs."""
    ext = ".jpg"
    if "image/png" in content_type: ext = ".png"
    elif "image/webp" in content_type: ext = ".webp"
    elif "application/pdf" in content_type: ext = ".pdf"
    elif "audio/" in content_type:
        ext = ".ogg"
        if "mpeg" in content_type: ext = ".mp3"
        elif "wav" in content_type: ext = ".wav"
        
    key = f"{folder}/{uuid.uuid4()}{ext}"
    
    _s3.put_object(
        Bucket=settings.AWS_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=content_type
    )
    url = f"https://{settings.AWS_BUCKET_NAME}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"
    return key, url

async def process_whatsapp_passport(sender_id: str, media_id: str, villa_code: str = "UNKNOWN", guest_name: str = None, customer_id: str = None, guest_id: str = None):
    """Downloads WA media, uploads to S3, and saves as a pending passport."""
    try:
        file_bytes, content_type = await download_whatsapp_media(media_id)
        s3_key, s3_url = upload_bytes_to_s3(file_bytes, content_type, folder="passports")

        final_guest_name = guest_name or f"WhatsApp Guest {sender_id[-4:]}"

        # PASSPORT-DIRECT-ATTACH-NAME-01 (2026-09-07): the guided WhatsApp
        # flow's "awaiting_name" step (and now also the direct-attach
        # "awaiting_name_for_pending_media" step) collects a real, guest-
        # typed name every submission -- but that name was NEVER persisted
        # back to guest_profile_collection, so nothing was ever available
        # for _resolve_villa_and_guest_for_passport() to find on a LATER
        # resubmission (e.g. after a rejection). This is the root cause a
        # resubmission could silently fall back to the generic
        # "WhatsApp Guest 1234" placeholder above, or to an unrelated name
        # from a different flow (registration/booking) for the same phone.
        # A narrow, full_name-only upsert here (never touching villa_code/
        # check_in_date/check_out_date, which this flow doesn't have and
        # which would incorrectly imply the guest went through full
        # registration) means later submissions -- via EITHER WhatsApp path
        # -- have a real name to fall back to instead of the placeholder.
        # Only writes when `guest_name` was an actual, real typed value
        # (never for the placeholder itself, which must never be persisted
        # as if it were the guest's real name).
        if guest_name and guest_name.strip():
            try:
                await guest_profile_collection.update_one(
                    {"phone_number": sender_id},
                    {
                        "$set": {"full_name": guest_name.strip(), "last_active_at": datetime.now(timezone.utc)},
                        "$setOnInsert": {
                            "guest_id": str(uuid.uuid4()),
                            "phone_number": sender_id,
                            "source": "whatsapp",
                            "created_at": datetime.now(timezone.utc),
                        },
                    },
                    upsert=True,
                )
            except Exception as _profile_write_err:
                logger.warning(f"guest_profile_collection name persist failed (non-fatal) for {sender_id}: {_profile_write_err}")

        # WCR-VILLA-MISMATCH-01 (2026-08-25): the web passport upload path
        # (passport_routes.py) always stores a "guest_id" field so a
        # resubmission by the same guest can be correlated to the same
        # identity. The WhatsApp path never set one at all, so a guest who
        # resubmitted (e.g. after a rejection) had no way to be recognised as
        # the same person across submissions. guest_id is resolved lazily by
        # the caller (guest_profile_collection lookup by phone) -- None here
        # just means "not registered yet", matching the web flow's own
        # pending_identity pattern; it is never required to accept the upload.
        passport_data = {
            "user_id": sender_id,
            "customer_id": customer_id,
            "guest_id": guest_id,
            "villa_code": villa_code,
            "guest_name": final_guest_name,
            "passport_url": s3_url,
            "s3_key": s3_key,
            "status": "pending_verification",
            "uploaded_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(days=90),
            "source": "whatsapp"
        }
        await passport_collection.insert_one(passport_data)
        logger.info(f"Passport for {sender_id} saved from WhatsApp. URL: {s3_url}")

        # NOTIF-VM-01 (2026-08-25): passport submission never notified the
        # villa manager on either channel, unlike amenities/issues (which
        # already do this). Lazy import -- whatsapp_func.py imports
        # process_whatsapp_passport at module level, so a top-level import
        # here would be circular. Non-fatal: the passport is already saved.
        #
        # PASSPORT-WA-STAFF-NOTIFIED-HONESTY-01 (2026-09-04): this block
        # unconditionally returned "submitted successfully" regardless of
        # whether the VM was actually reached -- unlike the web upload path
        # (passport_routes.py), which tracks staff_notified honestly and
        # lets PassportSubmission.jsx tell the guest the truth ("villa team
        # notified" vs "we couldn't reach the villa team automatically").
        # staff_notified now mirrors that same honest tracking here, and the
        # returned message text (the only thing this function's callers
        # forward to the guest -- see the two call sites in
        # whatsapp_func.py, both of which just relay `_msg`/`msg` verbatim)
        # varies accordingly, matching the web path's wording.
        staff_notified = False
        try:
            from app.utils.whatsapp_func import get_villa_whatsapp_by_code, send_whatsapp_message
            from app.services.menu_services import get_villa_info_by_code
            villa_info = await get_villa_info_by_code(villa_code)
            villa_name = (villa_info or {}).get("name") or villa_code
            villa_phone = await get_villa_whatsapp_by_code(villa_code)
            if villa_phone:
                staff_msg = (
                    f"🛂 *New Passport Submission*\n\n"
                    f"• *Villa:* {villa_name}\n"
                    f"• *Guest:* {final_guest_name}\n\n"
                    f"Please review and verify in the dashboard."
                )
                # PASSPORT-VM-NOTIFY-SILENT-FAIL-01 (2026-09-07): the return
                # value of send_whatsapp_message() was previously discarded
                # entirely -- staff_notified was set True unconditionally,
                # right after the call, regardless of whether Meta actually
                # accepted the message. send_whatsapp_message is a bare
                # freeform text send with NO template fallback; Meta silently
                # rejects (returns False, no exception) any freeform send to
                # a recipient whose 24-hour WhatsApp session window is closed
                # -- exactly the common case for a villa manager who doesn't
                # message the bot number daily. The result: a passport could
                # be submitted, staff_notified=True logged/returned, and the
                # villa manager never actually received anything, with zero
                # visible failure anywhere. No Meta-approved template exists
                # yet for this specific message (would need a multi-day Meta
                # submission to get a real window-bypass fallback), so this
                # fix makes the failure honest and observable instead of
                # silently claiming success.
                _vm_sent = await send_whatsapp_message(villa_phone, staff_msg)
                if _vm_sent:
                    logger.info(f"Villa staff notified via WhatsApp: {villa_phone} for passport ({sender_id})")
                    staff_notified = True
                else:
                    logger.warning(
                        f"Villa staff notification FAILED (Meta rejected send, likely 24-hour "
                        f"window closed) for {villa_code} -> {villa_phone}, passport ({sender_id})"
                    )
            else:
                logger.warning(f"No villa WhatsApp number found for {villa_code} — staff not notified")
        except Exception as notify_err:
            logger.warning(f"Villa staff notification failed (non-fatal): {notify_err}")

        if staff_notified:
            return True, "Your passport has been submitted successfully and is pending verification. Welcome!"
        return True, (
            "Your passport has been submitted successfully and is pending verification. "
            "We couldn't reach the villa team automatically, but your submission is saved and will still be reviewed."
        )
    except Exception as e:
        logger.error(f"Failed to process WhatsApp passport {media_id}: {e}")
        return False, "Sorry, there was an issue processing your document. Please try again later."

async def process_whatsapp_issue(sender_id: str, media_id: str, villa_code: str, description: str, media_type: str = "image", customer_id: str = None):
    """Handles issue reporting with media attachments. Transcribes if voice note."""
    try:
        file_bytes, content_type = await download_whatsapp_media(media_id)
        
        # New: Transcription for Voice Notes
        transcript = None
        if media_type == "voice_note":
            try:
                from app.services.openai_client import client
                import io
                
                logger.info(f"Transcribing voice note for {sender_id}...")
                audio_file = io.BytesIO(file_bytes)
                audio_file.name = "voice_note.ogg"
                
                response = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file
                )
                transcript = f"🎙️ (Voice Note): {response.text}"
                description = transcript
                logger.info(f"Transcription complete: {transcript[:50]}...")
            except Exception as te:
                logger.error(f"Transcription failed for {sender_id}: {te}")

        s3_key, s3_url = upload_bytes_to_s3(file_bytes, content_type, folder="issues")
        
        issue_data = {
            "sender_id": sender_id,
            "customer_id": customer_id,
            "villa_code": villa_code,
            "description": description,
            "media_url": s3_url,
            "s3_key": s3_key,
            "media_type": media_type,
            "status": "open",
            "source": "whatsapp",
            "timestamp": datetime.utcnow()
        }
        await issue_collection.insert_one(issue_data)
        logger.info(f"Issue for villa {villa_code} reported with {media_type}. Source: whatsapp. URL: {s3_url}")
        return True, s3_url, transcript
    except Exception as e:
        logger.error(f"Failed to process WhatsApp issue {media_id}: {e}")
        return False, None, None
