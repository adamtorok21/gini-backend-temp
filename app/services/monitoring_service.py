from app.db.session import db
from datetime import datetime, timedelta, UTC
from uuid import uuid4
import os
import time

system_error_collection = db["system_errors"]

async def store_system_error(
    trace_id: str,
    module: str,
    message: str,
    severity: str = "ERROR",
    endpoint: str = None,
    stack_trace: str = None,
    guest_id: str = None,
    order_number: str = None,
    extra: dict = None
):
    """Persists an error to the dashboard collection."""
    # DE-DUPLICATION: Don't store same error for same trace within 10s
    dup_window = datetime.now(UTC) - timedelta(seconds=10)
    existing = await system_error_collection.find_one({
        "trace_id": trace_id,
        "error_message": message,
        "created_at": {"$gte": dup_window}
    })
    if existing:
        return # Duplicate within request chain
    
    error_doc = {
        "error_id": str(uuid4())[:8].upper(),
        "trace_id": trace_id,
        "module": module,
        "endpoint": endpoint,
        "severity": severity,
        "error_message": message,
        "stack_trace": stack_trace,
        "guest_id": guest_id,
        "order_number": order_number,
        "extra": extra,
        "created_at": datetime.now(UTC),
        "resolved": False
    }
    
    try:
        await system_error_collection.insert_one(error_doc)
        
        # Trigger Alerting if CRITICAL
        if severity == "CRITICAL":
            await trigger_admin_alert(error_doc)
            
    except Exception as e:
        # Avoid infinite loops if logging fails
        print(f"FAILED TO STORE SYSTEM ERROR: {e}")

async def trigger_admin_alert(error_doc: dict):
    """Triggers admin alerts via WhatsApp for critical failures."""
    from app.utils.whatsapp_func import send_whatsapp_message
    from app.settings.config import settings
    
    admin_phones_raw = getattr(settings, "ADMIN_PHONE_NUMBERS", "") or ""
    admin_phones = [p.strip() for p in admin_phones_raw.split(",") if p.strip()]
    admin_phone = admin_phones[0] if admin_phones else None
    if not admin_phone:
        return

    # De-duplication / Cooldown logic
    # Check if a similar error happened recently
    cooldown_window = datetime.now(UTC) - timedelta(minutes=5)
    existing_alert = await system_error_collection.find_one({
        "module": error_doc["module"],
        "severity": "CRITICAL",
        "created_at": {"$gte": cooldown_window},
        "error_id": {"$ne": error_doc["error_id"]}
    })
    
    if existing_alert:
        return # Within cooldown

    alert_msg = (
        f"🚨 *CRITICAL SYSTEM FAILURE*\n\n"
        f"*Module:* {error_doc['module']}\n"
        f"*Error:* {error_doc['error_message']}\n"
        f"*Trace ID:* {error_doc['trace_id']}\n"
        f"*Time:* {error_doc['created_at'].strftime('%H:%M:%S')}\n\n"
        f"👉 Check Admin Dashboard for details."
    )
    
    try:
        await send_whatsapp_message(admin_phone, alert_msg)
    except Exception:
        pass

async def get_health_summary():
    """Aggregates error counts for the dashboard."""
    now = datetime.now(UTC)
    last_1h = now - timedelta(hours=1)
    last_24h = now - timedelta(hours=24)
    
    total_1h = await system_error_collection.count_documents({"created_at": {"$gte": last_1h}})
    total_24h = await system_error_collection.count_documents({"created_at": {"$gte": last_24h}})
    critical_24h = await system_error_collection.count_documents({
        "severity": "CRITICAL", 
        "created_at": {"$gte": last_24h}
    })
    
    # Most frequent failing module
    pipeline = [
        {"$match": {"created_at": {"$gte": last_24h}}},
        {"$group": {"_id": "$module", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 1}
    ]
    cursor = system_error_collection.aggregate(pipeline)
    freq_result = await cursor.to_list(length=1)
    top_module = freq_result[0]["_id"] if freq_result else "None"
    
    return {
        "errors_last_1h": total_1h,
        "errors_last_24h": total_24h,
        "critical_last_24h": critical_24h,
        "top_failing_module": top_module
    }
