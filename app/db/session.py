from motor.motor_asyncio import AsyncIOMotorClient
from app.settings.config import settings

import certifi
import urllib.parse

# Atlas URL construction
_uri = settings.MONGO_URII
# Only append parameters if it's a standard mongodb:// URI. 
# srv URIs handle tls and timeouts natively in Atlas.
if not _uri.startswith("mongodb+srv://"):
    if "?" in _uri:
        _uri += "&tls=true&tlsAllowInvalidCertificates=true&serverSelectionTimeoutMS=5000&connectTimeoutMS=10000"
    else:
        _uri += "?tls=true&tlsAllowInvalidCertificates=true&serverSelectionTimeoutMS=5000&connectTimeoutMS=10000"

import asyncio
_clients = {}

def get_client():
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # Fallback for non-async contexts or brand new loops
        return AsyncIOMotorClient(_uri, tlsCAFile=certifi.where())
        
    if loop not in _clients:
        _clients[loop] = AsyncIOMotorClient(_uri, tlsCAFile=certifi.where())
    return _clients[loop]

def get_db():
    db_name = settings.DB_NAME
    if not db_name:
        raise RuntimeError("CRITICAL ERROR: DB_NAME environment variable is not set. System cannot start.")
    
    return get_client().get_database(db_name)

class DatabaseProxy:
    def __getattr__(self, name):
        return getattr(get_db(), name)
    
    def __getitem__(self, name):
        return get_db()[name]

db = DatabaseProxy()

class CollectionProxy:
    def __init__(self, name):
        self._name = name
    
    def __getattr__(self, name):
        return getattr(get_db()[self._name], name)
        
    def __getitem__(self, name):
        return get_db()[self._name][name]

order_collection = CollectionProxy("orders-summary")
villa_code_collection = CollectionProxy('villa-codes')
passport_collection = CollectionProxy('passports')
checkin_collection = CollectionProxy('checkins')
issue_collection = CollectionProxy('issues')
inquiry_collection = CollectionProxy('inquiries')
feedback_collection = CollectionProxy('feedback')
content_collection = CollectionProxy('content_library')
customer_collection = CollectionProxy('customers')
guest_profile_collection = CollectionProxy('guest_profiles')
# VILLA-FRESH-CONTEXT-01 (2026-08-28): audit trail for guest_profile.villa_code
# corrections applied when a guest explicitly confirms a new villa on a later
# visit (e.g. V1 in June, V3 in August) — never used for resolution itself,
# purely for visibility into how often/where this override fires.
villa_change_log_collection = CollectionProxy('villa_change_log')
booking_sessions_collection = CollectionProxy('booking-sessions')
villa_code_sessions_collection = CollectionProxy('villa-code-sessions')
amenities_collection = CollectionProxy('amenities')
system_error_collection = CollectionProxy('system_errors')
webhook_dedupe_collection = CollectionProxy('whatsapp_webhook_dedupe')
rate_limit_collection = CollectionProxy('whatsapp_sender_rate_limits')
notification_log_collection = CollectionProxy('notification_log')
# Delivery receipts for OUTBOUND WhatsApp messages (Meta 'statuses' webhook:
# sent/delivered/read/failed). Keyed by Meta message_id. Records whether a
# notification actually reached the recipient's device, beyond "Meta accepted it".
notification_delivery_collection = CollectionProxy('whatsapp_delivery_status')
knowledge_base_collection = CollectionProxy('knowledge_base')
test_runs_collection = CollectionProxy('test_runs')
test_results_collection = CollectionProxy('test_results')
ai_usage_collection = CollectionProxy('ai_usage_daily')
# Cache for machine-translated sheet-driven guest-facing text (WCR-I18N-DYNAMIC-01).
# Keyed by sha256(target_lang + source_text) so repeat lookups never re-call the
# translation provider for text that hasn't changed.
translation_cache_collection = CollectionProxy('translation_cache')