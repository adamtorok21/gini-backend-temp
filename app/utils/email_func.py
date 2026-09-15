import aiosmtplib
from email.message import EmailMessage
import logging
import os

logger = logging.getLogger(__name__)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

async def send_email(to_email: str, subject: str, body: str, is_html: bool = False):
    if not SMTP_USER or not SMTP_PASS:
        logger.warning("SMTP credentials not configured. Skipping email send.")
        logger.info(f"Mock Email to {to_email}: {subject}\n{body}")
        return True

    message = EmailMessage()
    message["From"] = SMTP_USER
    message["To"] = to_email
    message["Subject"] = subject
    
    if is_html:
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)

    try:
        await aiosmtplib.send(
            message,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            start_tls=True,
            username=SMTP_USER,
            password=SMTP_PASS,
        )
        logger.info(f"Successfully sent email to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False
