from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import asyncio
import httpx
from app.db.session import db
from app.settings.config import settings

class WhatsAppQueue:
    def __init__(self):
        self.collection = db["whatsapp_message_queue"]
        self.max_retries = 3
        self.retry_delay = 5  # seconds

    async def enqueue(self, recipient_id: str, payload: Dict[str, Any], message_type: str = "text"):
        """Add a message to the queue"""
        message_data = {
            "recipient_id": recipient_id,
            "payload": payload,
            "message_type": message_type,
            "status": "pending",
            "retry_count": 0,
            "errors": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        result = await self.collection.insert_one(message_data)
        return str(result.inserted_id)

    async def process_queue(self):
        """Background worker to process pending messages"""
        while True:
            try:
                # Find pending/failed/retriable messages that still have retries.
                # "retry_pending" MUST be included — send_message_with_retry sets
                # that status on a retriable failure, so omitting it left failed
                # messages stuck forever (never re-attempted). The created_at age
                # guard stops long-stuck messages from resurrecting and delivering
                # a stale, out-of-context notification once the transient cause
                # clears (e.g. a template that was missing when first enqueued).
                cutoff = datetime.utcnow() - timedelta(hours=1)
                messages = self.collection.find({
                    "status": {"$in": ["pending", "failed", "retry_pending"]},
                    "retry_count": {"$lt": self.max_retries},
                    "created_at": {"$gte": cutoff},
                }).sort("created_at", 1).limit(10)

                async for msg in messages:
                    await self.send_message_with_retry(msg)
                
                await asyncio.sleep(10)  # Check every 10 seconds
            except Exception as e:
                print(f"Error in WhatsApp queue processor: {e}")
                await asyncio.sleep(30)

    async def send_message_with_retry(self, msg_record: Dict[str, Any]):
        msg_id = msg_record["_id"]
        recipient_id = msg_record["recipient_id"]
        payload = msg_record["payload"]
        
        headers = {
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(settings.whatsapp_api_url, json=payload, headers=headers)
                response.raise_for_status()
                
                # Success
                await self.collection.update_one(
                    {"_id": msg_id},
                    {
                        "$set": {
                            "status": "sent",
                            "sent_at": datetime.utcnow(),
                            "updated_at": datetime.utcnow()
                        }
                    }
                )
                print(f"✅ Message sent from queue to {recipient_id}")
                await self._log_send(recipient_id, payload, success=True)

        except Exception as e:
            error_msg = str(e)
            retry_count = msg_record["retry_count"] + 1
            status = "failed" if retry_count >= self.max_retries else "retry_pending"

            await self.collection.update_one(
                {"_id": msg_id},
                {
                    "$set": {
                        "status": status,
                        "retry_count": retry_count,
                        "updated_at": datetime.utcnow()
                    },
                    "$push": {
                        "errors": {
                            "attempt": retry_count,
                            "error": error_msg,
                            "timestamp": datetime.utcnow()
                        }
                    }
                }
            )
            print(f"❌ Failed to send message to {recipient_id} (Attempt {retry_count}): {error_msg}")
            await self._log_send(recipient_id, payload, success=False, error_detail=error_msg[:300])

    @staticmethod
    async def _log_send(recipient_id, payload, success, error_detail=None):
        """Write every queue send attempt to notification_log so automated
        sequences are traceable (no silent sends). Non-fatal — a logging
        failure must never affect delivery or the queue loop."""
        try:
            from app.utils.notification_logger import log_notification_attempt
            _tmpl = (payload.get("template") or {}).get("name") if payload.get("type") == "template" else None
            await log_notification_attempt(
                recipient=recipient_id,
                notification_type=_tmpl or "queue_text",
                channel="template" if _tmpl else "freeform",
                success=success,
                template_name=_tmpl,
                error_detail=error_detail,
            )
        except Exception:
            pass

# Global instance
whatsapp_queue = WhatsAppQueue()

async def enqueue_whatsapp_message(recipient_id: str, message_text: str):
    """Simple helper to enqueue a freeform text message (works inside 24-hr window only)."""
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "text",
        "text": {"body": message_text}
    }
    return await whatsapp_queue.enqueue(recipient_id, payload, message_type="text")


async def enqueue_whatsapp_template(
    recipient_id: str,
    template_name: str,
    template_vars: list,
    language_code: str = "en",
    dedup_scope: str = None,
):
    """
    Enqueue an approved Meta UTILITY/MARKETING template message.

    Template messages bypass the 24-hour customer service window — they are
    the only reliable delivery mechanism for automated sequences sent to guests
    who may never have interacted with the GINI Bali WhatsApp business number.

    Args:
        recipient_id:  Guest's WhatsApp number (digits only, no +).
        template_name: Exact name of the Meta-approved template.
        template_vars: List of strings for {{1}}, {{2}}, ... placeholders.
        language_code: Template language (default: "en").
        dedup_scope:   Optional extra key component (e.g. villa_code) so the
                       "once per stay" dedup below distinguishes stays, not
                       just phone+template. See WCR-GJL-32 in CLAUDE.md.
    """
    # Idempotency safety net for guest-journey sequences (templates ending in
    # "_seq"): each guest gets a given journey sequence at most once per stay. An
    # atomic insert into notification_dedup (unique key + ~14d TTL) makes a
    # duplicate send impossible even if two trigger records, two Render instances,
    # or a retry somehow reach this point — the final defense behind the per-record
    # atomic claim and the one-active-check-in unique index. Transactional
    # templates (no "_seq") are never deduped; they may legitimately repeat.
    #
    # WCR-GJL-32 (2026-08-17): the key was originally phone+template only, with
    # NO stay/villa component — so a guest re-registering for a DIFFERENT villa
    # within the 14-day TTL of a prior send was wrongly treated as a duplicate
    # of their old stay and silently skipped (root cause of "welcome message
    # not triggering after Guest Journey Link"). Callers that know which stay
    # they're sending for (currently: registration_welcome_seq) pass
    # dedup_scope so a new stay is never conflated with an old one. Callers
    # that omit it keep the original phone+template key — unchanged behavior
    # for the other automation sequences, which were not reported broken.
    if template_name.endswith("_seq"):
        from pymongo.errors import DuplicateKeyError
        _dedup_key = f"{recipient_id}:{template_name}"
        if dedup_scope:
            _dedup_key = f"{_dedup_key}:{dedup_scope}"
        try:
            await db["notification_dedup"].insert_one({
                "dedup_key": _dedup_key,
                "recipient_id": recipient_id,
                "template_name": template_name,
                "created_at": datetime.utcnow(),
            })
        except DuplicateKeyError:
            print(f"⏭️  Skipped duplicate sequence '{template_name}' for {recipient_id} (already sent this stay)")
            return None
        except Exception as _dd_err:
            # Dedup collection unavailable — do NOT block the send; the atomic
            # claim + unique index remain the primary guards.
            print(f"notification_dedup check failed (non-fatal): {_dd_err}")

    payload = {
        "messaging_product": "whatsapp",
        "to": recipient_id,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(v)} for v in template_vars],
                }
            ],
        },
    }
    return await whatsapp_queue.enqueue(recipient_id, payload, message_type="template")
