import logging
import logging.handlers
import contextvars
from uuid import uuid4
import json
from datetime import datetime, UTC
import re
import queue
import threading
from app.utils.async_utils import safe_create_task

# Non-blocking log processing
log_queue = queue.Queue(-1)
_log_listener = None

def setup_non_blocking_logging():
    global _log_listener
    if _log_listener:
        return
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    
    _log_listener = logging.handlers.QueueListener(log_queue, console_handler)
    _log_listener.start()

# Context variables for traceability
trace_id_var = contextvars.ContextVar("trace_id", default="SYSTEM")
guest_id_var = contextvars.ContextVar("guest_id", default=None)
order_number_var = contextvars.ContextVar("order_number", default=None)

SENSITIVE_FIELDS = {
    'phone_number', 'phone', 'contact_phone', 'account_number', 
    'whatsapp_number', 'token', 'access_token', 'api_key', 
    'secret', 'password', 'payment_url', 'download_url'
}

def mask_value(field, value):
    if value is None:
        return None
    val_str = str(value)
    if field in ('phone_number', 'phone', 'contact_phone', 'whatsapp_number'):
        if len(val_str) > 7:
            return f"{val_str[:5]}...{val_str[-3:]}"
        return "***"
    if field in ('token', 'access_token', 'api_key', 'secret', 'password', 'account_number'):
        return "***MASKED***"
    if field in ('payment_url', 'download_url'):
        return "URL_HIDDEN"
    return value

def mask_sensitive_data(data):
    if isinstance(data, dict):
        return {k: mask_sensitive_data(v) if k not in SENSITIVE_FIELDS else mask_value(k, v) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_sensitive_data(i) for i in data]
    return data

# Regex to match potential phone numbers (e.g. +62..., 0812...)
PHONE_REGEX = re.compile(r'(\+?\d{1,4}[-.\s]?)?\(?\d{2,4}?\)?[-.\s]?\d{3,4}[-.\s]?\d{4,6}')

def mask_text(text):
    if not isinstance(text, str):
        return text
    # Masking phone numbers in arbitrary text strings
    def repl(m):
        raw = m.group(0)
        # Only mask if it looks like a real phone number length
        digits = re.sub(r'\D', '', raw)
        if 8 <= len(digits) <= 15:
            return f"{raw[:5]}...{raw[-3:]}"
        return raw
    return PHONE_REGEX.sub(repl, text)

class StructuredLogger:
    def __init__(self, name):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        # Offload all logs to the non-blocking queue
        if not any(isinstance(h, logging.handlers.QueueHandler) for h in self.logger.handlers):
            self.logger.addHandler(logging.handlers.QueueHandler(log_queue))

    def _build_log_entry(self, tag, message, extra=None):
        trace_id = trace_id_var.get()
        guest_id = guest_id_var.get()
        order_number = order_number_var.get()
        
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "trace_id": trace_id,
            "tag": tag,
            "message": mask_text(message), # Auto-mask phone numbers in strings
            "guest_id": guest_id,
            "order_number": order_number
        }
        
        if extra:
            # Mask sensitive data in extra fields before logging
            masked_extra = mask_sensitive_data(extra)
            entry.update(masked_extra)
            
        return entry

    def info(self, tag, message, extra=None):
        entry = self._build_log_entry(tag, message, extra)
        self.logger.info(json.dumps(entry))

    def warning(self, tag, message, extra=None):
        entry = self._build_log_entry(tag, message, extra)
        # Standard level is WARNING
        self.logger.warning(json.dumps(entry))

    def error(self, tag, message, extra=None, exc_info=True):
        entry = self._build_log_entry(tag, message, extra)
        # Use exc_info=True to capture traceback in system logs privately
        self.logger.error(json.dumps(entry), exc_info=exc_info)
        
        # PERSIST TO DB (Non-blocking helper)
        self._safe_create_task(tag, message, "ERROR", entry, extra, exc_info)

    def critical(self, tag, message, extra=None):
        entry = self._build_log_entry(tag, message, extra)
        self.logger.critical(json.dumps(entry))
        
        # PERSIST TO DB (Non-blocking helper)
        self._safe_create_task(tag, message, "CRITICAL", entry, extra)



    def _safe_create_task(self, tag, message, severity, entry, extra=None, exc_info=False):
        """Helper to create a task only if a running loop exists and is not closed."""
        from app.services.monitoring_service import store_system_error
        import traceback
        
        safe_create_task(store_system_error(
            trace_id=entry["trace_id"],
            module=tag,
            message=message,
            severity=severity,
            stack_trace=traceback.format_exc() if exc_info else None,
            guest_id=entry["guest_id"],
            order_number=entry["order_number"],
            extra=extra
        ))

def get_logger(name):
    return StructuredLogger(name)

def set_logging_context(guest_id=None, order_number=None):
    if guest_id:
        guest_id_var.set(guest_id)
    if order_number:
        order_number_var.set(order_number)
