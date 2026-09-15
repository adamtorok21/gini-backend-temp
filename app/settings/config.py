"""
RECONSTRUCTED config module (sandbox prototype).

The original `app/settings/config.py` was NOT included in the developer's handover,
but every module imports `from app.settings.config import settings`. This file was
rebuilt by inferring the exact attribute names and types from how `settings.X` is used
across the codebase, so the app can import and boot. Real values come from a .env file
(or the environment). Defaults here are safe placeholders only.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ── Database ──────────────────────────────────────────────
    MONGO_URII: str = "mongodb://localhost:27017"          # note: original var name has double-I
    DB_NAME: str = "easybali"

    # ── Auth ──────────────────────────────────────────────────
    JWT_SECRET_KEY: str = "CHANGE_ME_dev_only_secret"
    ADMIN_PHONE_NUMBERS: str = ""                           # comma-separated digits

    # ── OpenAI ────────────────────────────────────────────────
    OPENAI_API_KEY: str = ""
    OPENAI_API_KEY_TEST: str = ""
    OPENAI_MODEL_NAME: str = "gpt-4o-mini"
    OPENAI_MAX_INPUT_CHARS: int = 12000

    # ── AI budget guard ──────────────────────────────────────
    AI_ENABLED: bool = True
    AI_EMERGENCY_KILL_SWITCH: bool = False
    AI_DAILY_REQUEST_LIMIT: int = 10000
    AI_DAILY_TOKEN_LIMIT: int = 5_000_000
    ALLOW_REAL_OPENAI_IN_TESTS: bool = False

    # ── Pinecone (vector DB / RAG) ───────────────────────────
    pinecone_api_key: str = ""
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    # ── AWS S3 ───────────────────────────────────────────────
    AWS_ACCESS_KEY: str = ""
    AWS_SECRET_KEY: str = ""
    AWS_REGION: str = "ap-southeast-2"
    AWS_BUCKET_NAME: str = "easybali"

    # ── Xendit (payments) ────────────────────────────────────
    XENDIT_SECRET_KEY: str = ""
    XENDIT_WEBHOOK_BASE_URL: str = ""
    XENDIT_INVOICE_DURATION_SECONDS: int = 86400
    ENABLE_LIVE_DISBURSEMENT: bool = False

    # ── WhatsApp / Meta ──────────────────────────────────────
    access_token: str = ""                                  # Meta Cloud API bearer token
    whatsapp_api_url: str = "https://graph.facebook.com/v21.0/"
    WHATSAPP_WABA_ID: str = ""
    WHATSAPP_CATEGORY_FLOW_ID: str = ""
    WHATSAPP_PRIVATE_KEY: str = ""
    WHATSAPP_PRIVATE_KEY_PASSWORD: str = ""

    # ── Google (Sheets service account) ──────────────────────
    GOOGLE_SERVICE_ACCOUNT_JSON: str = ""

    # ── URLs ─────────────────────────────────────────────────
    BASE_URL: str = "http://localhost:8000"
    WEB_BASE_URL: str = "http://localhost:5173"


settings = Settings()
