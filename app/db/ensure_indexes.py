import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
from app.settings.config import settings
import logging

logger = logging.getLogger(__name__)

async def ensure_indexes():
    client = AsyncIOMotorClient(settings.MONGO_URII)
    try:
        db = client.get_database('easybali')

        # 1. Orders Summary
        logger.info("Ensuring indexes for orders-summary...")
        await db["orders-summary"].create_index("order_number", unique=True)
        await db["orders-summary"].create_index("sender_id")
        await db["orders-summary"].create_index("status")

        # 2. Villa Codes
        logger.info("Ensuring indexes for villa-codes...")
        await db["villa-codes"].create_index("sender_id", unique=True)

        # 3. Villas (Onboarding)
        logger.info("Ensuring indexes for villas...")
        await db["villas"].create_index("villa_code", unique=True)

        # 4. WhatsApp Queue
        logger.info("Ensuring indexes for whatsapp_message_queue...")
        await db["whatsapp_message_queue"].create_index("status")
        await db["whatsapp_message_queue"].create_index("created_at")

        # 5. Latency Analytics
        logger.info("Ensuring indexes for analytics_latency...")
        await db["analytics_latency"].create_index("timestamp")

        # 6. Checkins
        logger.info("Ensuring indexes for checkins...")
        await db["checkins"].create_index("order_number")
        await db["checkins"].create_index("villa_code")
        await db["checkins"].create_index([("check_in_date", 1), ("check_out_date", 1)])

        # 6b. ONE active check-in per guest (the automation trigger record).
        # The butler drives all guest-journey notifications off checkins{status:active}.
        # Without a uniqueness guarantee, an upsert race across paths/instances can
        # create two active records for the same guest -> a sequence (e.g. day-1
        # welcome) fires twice. First de-duplicate any existing active records
        # (keep the earliest, merge their sent_automations so nothing re-fires,
        # supersede the rest), then enforce uniqueness with a partial unique index.
        try:
            pipeline = [
                {"$match": {"status": "active", "sender_id": {"$ne": None}}},
                {"$group": {"_id": "$sender_id", "ids": {"$push": "$_id"}, "count": {"$sum": 1}}},
                {"$match": {"count": {"$gt": 1}}},
            ]
            _merge_fields = ("checkin_date", "checkout_date", "estimated_checkout",
                             "guest_name", "villa_code", "villa_name", "location_zone", "guest_id")
            async for grp in db["checkins"].aggregate(pipeline):
                docs = await db["checkins"].find({"_id": {"$in": grp["ids"]}}).to_list(100)
                # Keep the BEST record: prefer one that has real dates, then the
                # earliest _id. Merge everything into it so no data (or sent-history)
                # is lost, then supersede the rest.
                docs.sort(key=lambda d: (
                    0 if d.get("checkin_date") else 1,
                    0 if d.get("checkout_date") else 1,
                    d["_id"],
                ))
                keep_doc = docs[0]
                keep = keep_doc["_id"]
                merged_sent, fill, earliest_ct = set(), {}, keep_doc.get("checkin_time")
                for d in docs:
                    merged_sent.update(d.get("sent_automations") or [])
                    for f in _merge_fields:
                        if not keep_doc.get(f) and d.get(f) and f not in fill:
                            fill[f] = d[f]
                    ct = d.get("checkin_time")
                    if ct and (earliest_ct is None or ct < earliest_ct):
                        earliest_ct = ct  # keep the earliest arrival clock
                set_fields = {"sent_automations": list(merged_sent), **fill}
                if earliest_ct and earliest_ct != keep_doc.get("checkin_time"):
                    set_fields["checkin_time"] = earliest_ct
                await db["checkins"].update_one({"_id": keep}, {"$set": set_fields})
                for d in docs:
                    if d["_id"] != keep:
                        await db["checkins"].update_one(
                            {"_id": d["_id"]}, {"$set": {"status": "superseded"}}
                        )
                logger.warning(f"checkins dedup: kept {keep}, superseded {len(docs)-1} for {grp['_id']}")

            await db["checkins"].create_index(
                "sender_id", unique=True,
                partialFilterExpression={"status": "active"},
                name="uniq_active_checkin_per_sender",
            )
        except Exception as _ck_err:
            logger.error(f"checkins active-uniqueness setup failed (non-fatal): {_ck_err}")

        # 7. Issues
        logger.info("Ensuring indexes for issues...")
        await db["issues"].create_index("status")
        await db["issues"].create_index("villa_code")
        await db["issues"].create_index("created_at")

        # 8. Passports
        logger.info("Ensuring indexes for passports...")
        await db["passports"].create_index("order_number")
        await db["passports"].create_index("villa_code")

        # 9. Refund Requests
        logger.info("Ensuring indexes for refund_requests...")
        await db["refund_requests"].create_index("order_number")
        await db["refund_requests"].create_index("status")
        await db["refund_requests"].create_index("created_at")

        # 10. Content Library
        logger.info("Ensuring indexes for content_library...")
        await db["content_library"].create_index("villa_code")
        await db["content_library"].create_index("category")

        # 11. Villa-code sessions (restart-safe onboarding state)
        logger.info("Ensuring indexes for villa-code-sessions...")
        await db["villa-code-sessions"].create_index("sender_id", unique=True)
        # TTL: auto-expire stale sessions after 72 hours (prevents unbounded growth)
        await db["villa-code-sessions"].create_index(
            "updated_at", expireAfterSeconds=259200
        )

        # 11b. Notification dedup — idempotency for guest-journey sequences.
        # A unique dedup_key (recipient:template) makes a duplicate _seq send
        # impossible; the TTL (~14 days ≈ one stay) lets a genuine later stay
        # receive the sequence again.
        logger.info("Ensuring indexes for notification_dedup...")
        await db["notification_dedup"].create_index("dedup_key", unique=True)
        await db["notification_dedup"].create_index(
            "created_at", expireAfterSeconds=1209600
        )

        # 12. Booking sessions (TTL: 24 hours)
        logger.info("Ensuring indexes for booking-sessions...")
        await db["booking-sessions"].create_index("sender_id", unique=True)
        await db["booking-sessions"].create_index(
            "updated_at", expireAfterSeconds=86400
        )

        logger.info("✅ All indexes ensured successfully!")
    finally:
        client.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(ensure_indexes())
