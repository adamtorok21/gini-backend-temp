import uvicorn
from fastapi import FastAPI
from app.routes.main_menu_routes import router as menu_router
from app.routes.chatbot_routes import router as chat_router
from app.routes.whatsapp_routes import router as whatsapp_router
from app.routes.things_to_do_in_Bali import router as things_bali
from app.routes.event_calender import router as event_calender
from app.routes.local_cuisine import router as local_cuisine
from app.routes.what_to_do import router as what_to_do
from app.routes.plan_my_trip import router as plan_my_trip
from app.routes.language_lesson import router as language_lesson
from app.routes.websockett import router as web_order_flow
from app.routes.currency_route import router as currency_converter
from app.routes.villa_links import router as villa_links_router
from app.routes.passport_routes import router as passport_router
from app.routes.issue_routes import router as issue_router
from app.routes.amenity_routes import router as amenity_router
from app.routes.onboarding import router as onboarding_router
from app.routes.monitoring_routes import router as monitoring_router
from app.routes.admin_users import router as admin_users_router
from app.routes.promo_admin import router as promo_admin_router
from app.routes.faq_admin import router as faq_admin_router
from app.routes.automation_admin import router as automation_admin_router
from app.routes.dashboard_routes import router as dashboard_router
from app.routes.admin_routes import router as admin_router
from app.routes.whatsapp_flows_routes import router as whatsapp_flows_router
from app.routes.guest_routes import router as guest_router
from app.routes.service_inquiry import router as service_inquiry_router
from app.routes.admin_control_routes import router as admin_control_router
from app.routes.dnp_routes import router as dnp_router
from fastapi.middleware.cors import CORSMiddleware
from app.services.menu_services import start_cache_refresh, stop_cache_refresh
from app.services.automation_butler import process_automations
from app.services.feature_auditor import start_auditor_loop
import logging
import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from app.utils.logger import trace_id_var, get_logger, setup_non_blocking_logging
from uuid import uuid4
import traceback

setup_non_blocking_logging()
logger = get_logger("main")


logging.basicConfig(level=logging.INFO)


app = FastAPI(
    title="Easy-Bali Chatbot",
    description="API's for easy-bali chatbot",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_trace_id_middleware(request: Request, call_next):
    # Retrieve trace_id from header (if frontend sends it) or generate new one
    trace_id = request.headers.get("X-Trace-ID", str(uuid4()))
    token = trace_id_var.set(trace_id)
    try:
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response
    finally:
        trace_id_var.reset(token)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # Content-Security-Policy (CSP)
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; connect-src 'self' https://api.openai.com https://*.xendit.co;"
    return response

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    trace_id = trace_id_var.get()

    # Capture full details for PRIVATE logs only
    error_details = {
        "endpoint": str(request.url),
        "method": request.method,
        "trace_id": trace_id,
        "error": str(exc),
        "stack_trace": traceback.format_exc()
    }

    # Log securely (StructuredLogger will JSON serialize this)
    logger.error("SYSTEM_ERROR", f"Critical failure: {str(exc)}", error_details)

    # Return GENERIC message to user
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": "Sorry, I encountered an issue processing your request. Please try again later.",
            "trace_id": trace_id
        }
    )



# Include routes
app.include_router(menu_router)
app.include_router(chat_router)
app.include_router(whatsapp_router)
app.include_router(things_bali)
app.include_router(event_calender)
app.include_router(local_cuisine)
app.include_router(what_to_do)
app.include_router(plan_my_trip)
app.include_router(language_lesson)
app.include_router(web_order_flow)
app.include_router(currency_converter)
app.include_router(villa_links_router)
app.include_router(passport_router)
app.include_router(issue_router)
app.include_router(amenity_router)
app.include_router(onboarding_router)
app.include_router(monitoring_router)
app.include_router(admin_users_router)
app.include_router(promo_admin_router)
app.include_router(faq_admin_router)
app.include_router(automation_admin_router)
app.include_router(dashboard_router)
app.include_router(admin_router)
app.include_router(whatsapp_flows_router)
app.include_router(guest_router)
app.include_router(service_inquiry_router)
app.include_router(admin_control_router, prefix="/dashboard-api")
app.include_router(dnp_router)

async def _try_register_wa_flow_key():
    """
    SOLUTION 3: At startup, attempt to register the WhatsApp Flow public key with Meta.
    This is a best-effort operation — if it fails (wrong token permissions, already registered),
    it logs a warning and continues. It does NOT block startup.
    """
    import httpx
    from app.settings.config import settings
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    raw_key = settings.WHATSAPP_PRIVATE_KEY
    if not raw_key:
        logging.warning("WHATSAPP_PRIVATE_KEY not set — WhatsApp Flow decryption will fail")
        return

    if "\\n" in raw_key:
        raw_key = raw_key.replace("\\n", "\n")

    password = (
        settings.WHATSAPP_PRIVATE_KEY_PASSWORD.encode("utf-8")
        if settings.WHATSAPP_PRIVATE_KEY_PASSWORD
        else None
    )
    try:
        private_key = load_pem_private_key(raw_key.encode("utf-8"), password=password)
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
    except Exception as e:
        logging.warning(f"Failed to load WHATSAPP_PRIVATE_KEY at startup: {e}")
        return

    waba_id = settings.WHATSAPP_WABA_ID
    if not waba_id:
        logging.warning("WHATSAPP_WABA_ID not set — cannot auto-register flow key")
        return

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://graph.facebook.com/v22.0/{waba_id}/business_encryption_key",
                headers={
                    "Authorization": f"Bearer {settings.access_token}",
                    "Content-Type": "application/json",
                },
                json={"business_public_key": public_pem},
            )
        if resp.status_code == 200:
            logging.info("✅ WhatsApp Flow public key registered with Meta successfully")
        else:
            logging.warning(
                f"⚠️ Could not auto-register WhatsApp Flow key: {resp.status_code} {resp.text[:200]}. "
                "Call POST /admin/register-wa-flow-key after updating access_token permissions."
            )
    except Exception as e:
        logging.warning(f"WhatsApp Flow key registration skipped: {e}")


@app.on_event("startup")
async def on_startup():
    # Eagerly ping MongoDB so the connection pool is ready before the first request.
    # Without this, Render cold-start serves GET / immediately but the first DB call
    # (e.g. /passports/upload → verify_guest) has to wait for motor to connect.
    try:
        from app.db.session import db, webhook_dedupe_collection, rate_limit_collection
        await db.command("ping")
        logging.info("✅ MongoDB connection established on startup")

        # Ensure indexes for deduplication
        await webhook_dedupe_collection.create_index("message_id", unique=True)
        await webhook_dedupe_collection.create_index("received_at", expireAfterSeconds=259200)
        logging.info("✅ Webhook dedupe indexes ensured")
        
        # Ensure indexes for rate limiter
        await rate_limit_collection.create_index("window_start", expireAfterSeconds=86400)
        logging.info("✅ Rate limiter TTL indexes ensured")
    except Exception as e:
        logging.warning(f"MongoDB startup ping or index creation failed (non-fatal): {e}")

    start_cache_refresh()
    asyncio.create_task(process_automations())
    asyncio.create_task(_try_register_wa_flow_key())
    asyncio.create_task(start_auditor_loop())
    from app.services.sp_timeout_checker import start_sp_timeout_checker
    asyncio.create_task(start_sp_timeout_checker())
    from app.services.booking_lifecycle import start_booking_lifecycle_worker
    asyncio.create_task(start_booking_lifecycle_worker())

    # Detect villa notification retry loops that were interrupted by a process restart.
    # Resets stuck active flags and alerts admin. Safe to run on every startup.
    from app.utils.whatsapp_func import detect_stuck_villa_retries
    asyncio.create_task(detect_stuck_villa_retries())
    # Embed Google Sheet data into Pinecone on startup (non-blocking)
    async def _embed_on_startup():
        await asyncio.sleep(5)  # Let cache finish loading first
        from app.services.embedding_service import embed_and_upsert_services
        from app.services.menu_services import cache
        await embed_and_upsert_services(cache)
    asyncio.create_task(_embed_on_startup())

@app.on_event("shutdown")
def on_shutdown():
    stop_cache_refresh()

@app.get("/")
def read_root():
    return {"msg": "Welcome to EASY-BALI chatbot"}

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 8003))
    uvicorn.run(app, host="0.0.0.0", port=port)
