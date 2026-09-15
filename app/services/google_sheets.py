import os
import json
import gspread
import logging
from google.oauth2.service_account import Credentials
from app.settings.config import settings

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_FILE = "easy-bali-b74b61110525.json"

def get_workbook(sheet_id: str):
    try:
        if settings.GOOGLE_SERVICE_ACCOUNT_JSON:
            logger.info("Using GOOGLE_SERVICE_ACCOUNT_JSON from settings")
            service_account_info = json.loads(settings.GOOGLE_SERVICE_ACCOUNT_JSON)
            creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
        elif os.path.exists(CREDENTIALS_FILE):
            logger.info(f"Using credentials file: {CREDENTIALS_FILE}")
            creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
        else:
            logger.error("❌ No Google Service Account credentials found! Set GOOGLE_SERVICE_ACCOUNT_JSON env var or add the JSON file.")
            raise FileNotFoundError("Google credentials missing.")
            
        client = gspread.authorize(creds)
        return client.open_by_key(sheet_id)
    except Exception as e:
        logger.error(f"Failed to load Google Sheet: {e}")
        raise e
