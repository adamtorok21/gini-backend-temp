"""
WhatsApp Flows service — GINI Bali booking form (Meta WhatsApp Flows v3).
"""
import base64
import json
import logging

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PHONE_NUMBER_ID = "681815231685748"
GRAPH_API_VERSION = "v22.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
WA_MESSAGES_URL = f"{GRAPH_BASE}/{PHONE_NUMBER_ID}/messages"

# ---------------------------------------------------------------------------
# Flow JSON definition
# ---------------------------------------------------------------------------
BOOKING_FLOW_JSON = {
    "version": "6.1",
    "screens": [
        {
            "id": "BOOKING",
            "title": "Book Your Service",
            "terminal": True,
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "TextInput",
                        "name": "full_name",
                        "label": "Full Name",
                        "required": True,
                        "helper-text": "Your name for the booking",
                    },
                    {
                        "type": "TextInput",
                        "name": "phone_number",
                        "label": "Phone Number",
                        "required": True,
                        "input-type": "phone",
                        "helper-text": "Please enter your WhatsApp number (e.g. +6281234567890).",
                    },
                    {
                        "type": "CalendarPicker",
                        "name": "booking_date",
                        "label": "Select Date",
                        "required": True,
                    },
                    {
                        "type": "RadioButtonsGroup",
                        "name": "time_slot",
                        "label": "Preferred Time Slot",
                        "required": True,
                        "data-source": [
                            {"id": "08:00-10:00", "title": "08:00 AM - 10:00 AM"},
                            {"id": "10:00-12:00", "title": "10:00 AM - 12:00 PM"},
                            {"id": "12:00-14:00", "title": "12:00 PM - 02:00 PM"},
                            {"id": "14:00-16:00", "title": "02:00 PM - 04:00 PM"},
                            {"id": "16:00-18:00", "title": "04:00 PM - 06:00 PM"},
                            {"id": "18:00-20:00", "title": "06:00 PM - 08:00 PM"},
                            {"id": "20:00-22:00", "title": "08:00 PM - 10:00 PM"},
                        ],
                    },
                    {
                        "type": "TextCaption",
                        "text": "Please select a 2-hour time window to make sure our service providers find a schedule for you.",
                    },
                    {
                        "type": "TextCaption",
                        "text": "By making the payment, you confirm that these details are correct. Please note that bookings cannot be canceled once confirmed. Kindly refer to our [Terms & Conditions](https://bali-zeta.vercel.app/terms-and-conditions) for more information.",
                        "markdown": True,
                    },
                    {
                        "type": "Footer",
                        "label": "Confirm Booking",
                        "on-click-action": {
                            "name": "complete",
                            "payload": {
                                "full_name": "${form.full_name}",
                                "phone_number": "${form.phone_number}",
                                "booking_date": "${form.booking_date}",
                                "time_slot": "${form.time_slot}",
                            },
                        },
                    },
                ],
            },
        }
    ],
}

GUEST_REGISTRATION_FLOW_JSON = {
    "version": "6.1",
    "screens": [
        {
            "id": "REGISTRATION",
            "title": "Welcome to Bali!",
            "terminal": True,
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "TextHeading",
                        "text": "Identify Yourself"
                    },
                    {
                        "type": "TextBody",
                        "text": "Please provide your details to start using our concierge services."
                    },
                    {
                        "type": "TextInput",
                        "name": "full_name",
                        "label": "Full Name",
                        "required": True,
                    },
                    {
                        "type": "TextInput",
                        "name": "villa_code",
                        "label": "Villa Code",
                        "required": True,
                        "helper-text": "Enter your villa code (e.g. EBV001)"
                    },
                    {
                        "type": "CalendarPicker",
                        "name": "check_in_date",
                        "label": "Check-In Date",
                        "required": True,
                    },
                    {
                        "type": "CalendarPicker",
                        "name": "check_out_date",
                        "label": "Check-Out Date",
                        "required": True,
                    },
                    {
                        "type": "Footer",
                        "label": "Register",
                        "on-click-action": {
                            "name": "complete",
                            "payload": {
                                "full_name": "${form.full_name}",
                                "villa_code": "${form.villa_code}",
                                "check_in_date": "${form.check_in_date}",
                                "check_out_date": "${form.check_out_date}"
                            },
                        },
                    },
                ],
            },
        }
    ],
}

CATEGORY_FLOW_JSON = {
    "version": "7.2",
    "data_api_version": "3.0",
    "routing_model": {
        "FIRST_SCREEN": ["SECOND_SCREEN"],
        "SECOND_SCREEN": ["SERVICE_SCREEN"],
        "SERVICE_SCREEN": ["BOOKING"],
        "BOOKING": [],
    },
    "screens": [
        {
            "id": "FIRST_SCREEN",
            "title": "Available Services",
            "data": {
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "main-content": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "metadata": {"type": "string"},
                                },
                            },
                        },
                    },
                    "__example__": [
                        {
                            "id": "Health & Wellness",
                            "main-content": {"title": "Health & Wellness", "metadata": "Spa treatments, massages and wellness services."},
                            "on-click-action": {"name": "data_exchange", "payload": {"selection": "Health & Wellness", "villa_code": "V1", "villa_name": "Villa Manila"}},
                        },
                        {
                            "id": "Transportation",
                            "main-content": {"title": "Transportation", "metadata": "Airport pickups, private drivers and day trips."},
                            "on-click-action": {"name": "data_exchange", "payload": {"selection": "Transportation", "villa_code": "V1", "villa_name": "Villa Manila"}},
                        },
                    ],
                },
                "villa_code": {"type": "string", "__example__": "V1"},
                "villa_name": {"type": "string", "__example__": "Villa Manila"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "NavigationList",
                        "name": "category_nav",
                        "label": "Select a category",
                        "list-items": "${data.categories}",
                    }
                ],
            },
        },
        {
            "id": "SECOND_SCREEN",
            "title": "Select Type",
            "data": {
                "sub_categories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "main-content": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "metadata": {"type": "string"},
                                },
                            },
                        },
                    },
                    "__example__": [
                        {
                            "id": "Massage",
                            "main-content": {"title": "Massage", "metadata": "Relaxing full-body and targeted massage treatments."},
                            "on-click-action": {"name": "data_exchange", "payload": {"subcategory_selection": "Massage", "selection": "Health & Wellness", "villa_code": "V1", "villa_name": "Villa Manila"}},
                        },
                        {
                            "id": "Yoga",
                            "main-content": {"title": "Yoga", "metadata": "Private sessions with expert instructors."},
                            "on-click-action": {"name": "data_exchange", "payload": {"subcategory_selection": "Yoga", "selection": "Health & Wellness", "villa_code": "V1", "villa_name": "Villa Manila"}},
                        },
                    ],
                },
                "selection": {"type": "string", "__example__": "Health & Wellness"},
                "villa_code": {"type": "string", "__example__": "V1"},
                "villa_name": {"type": "string", "__example__": "Villa Manila"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "NavigationList",
                        "name": "subcategory_nav",
                        "label": "${data.selection}",
                        "list-items": "${data.sub_categories}",
                    }
                ],
            },
        },
        {
            "id": "SERVICE_SCREEN",
            "title": "Select Service",
            "data": {
                "service_items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "main-content": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "metadata": {"type": "string"},
                                },
                            },
                        },
                    },
                    "__example__": [
                        {
                            "id": "Balinese Massage - 60min",
                            "main-content": {"title": "Balinese Massage - 60min", "metadata": "IDR 200,000 - Full body massage using oil cream."},
                        },
                    ],
                },
                "selection": {"type": "string", "__example__": "Health & Wellness"},
                "subcategory_selection": {"type": "string", "__example__": "Massage"},
                "flow_token": {"type": "string", "__example__": "token"},
                "villa_code": {"type": "string", "__example__": "V1"},
                "villa_name": {"type": "string", "__example__": "Villa Manila"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "NavigationList",
                        "name": "service_nav",
                        "label": "${data.subcategory_selection}",
                        "list-items": "${data.service_items}",
                    }
                ],
            },
        },
        {
            "id": "BOOKING",
            "title": "Book Your Service",
            "terminal": True,
            "data": {
                "service_name": {"type": "string", "__example__": "Balinese Massage - 60min"},
                "price": {"type": "string", "__example__": "IDR 200,000"},
                "selected_category": {"type": "string", "__example__": "Health & Wellness"},
                "selected_subcategory": {"type": "string", "__example__": "Massage"},
                "flow_token": {"type": "string", "__example__": "token"},
                "min_date": {"type": "string", "__example__": "2026-01-01"},
                "villa_code": {"type": "string", "__example__": "V1"},
                "villa_name": {"type": "string", "__example__": "Villa Manila"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {"type": "TextHeading", "text": "${data.service_name}"},
                    {"type": "TextBody", "text": "${data.price}"},
                    {
                        "type": "Form",
                        "name": "booking_form",
                        "children": [
                            {
                                "type": "TextInput",
                                "name": "full_name",
                                "label": "Full Name",
                                "required": True,
                                "helper-text": "Your name for the booking",
                            },
                            {
                                "type": "TextInput",
                                "name": "phone_number",
                                "label": "Phone Number",
                                "required": True,
                                "input-type": "phone",
                                "helper-text": "Please enter your WhatsApp number (e.g. +6281234567890).",
                            },
                            {
                                "type": "CalendarPicker",
                                "name": "booking_date",
                                "label": "Select Date",
                                "mode": "single",
                                "min-date": "${data.min_date}",
                                "include-days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                                "required": True,
                            },
                            {
                                "type": "RadioButtonsGroup",
                                "name": "time_slot",
                                "label": "Preferred Time Slot",
                                "required": True,
                                "data-source": [
                                    {"id": "08:00-10:00", "title": "08:00 AM - 10:00 AM"},
                                    {"id": "10:00-12:00", "title": "10:00 AM - 12:00 PM"},
                                    {"id": "12:00-14:00", "title": "12:00 PM - 02:00 PM"},
                                    {"id": "14:00-16:00", "title": "02:00 PM - 04:00 PM"},
                                    {"id": "16:00-18:00", "title": "04:00 PM - 06:00 PM"},
                                    {"id": "18:00-20:00", "title": "06:00 PM - 08:00 PM"},
                                    {"id": "20:00-22:00", "title": "08:00 PM - 10:00 PM"},
                                ],
                            },
                            {
                                "type": "TextCaption",
                                "text": "Please select a 2-hour time window to make sure our service providers find a schedule for you.",
                            },
                            {
                                "type": "TextCaption",
                                "text": "By making the payment, you confirm that these details are correct. Please note that bookings cannot be canceled once confirmed. Kindly refer to our [Terms & Conditions](https://bali-zeta.vercel.app/terms-and-conditions) for more information.",
                                "markdown": True,
                            },
                            {
                                "type": "Footer",
                                "label": "Confirm Booking",
                                "on-click-action": {
                                    "name": "complete",
                                    "payload": {
                                        "selected_service": "${data.service_name}",
                                        "selected_category": "${data.selected_category}",
                                        "selected_subcategory": "${data.selected_subcategory}",
                                        "flow_token": "${data.flow_token}",
                                        "villa_code": "${data.villa_code}",
                                        "villa_name": "${data.villa_name}",
                                        "full_name": "${form.full_name}",
                                        "phone_number": "${form.phone_number}",
                                        "booking_date": "${form.booking_date}",
                                        "time_slot": "${form.time_slot}",
                                    },
                                },
                            },
                        ],
                    },
                ],
            },
        },
    ],
}

# ---------------------------------------------------------------------------
# Admin helpers — create / publish / inspect flows
# ---------------------------------------------------------------------------

def _auth_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }


async def create_booking_flow(waba_id: str) -> dict:
    """
    1. Create a new flow under waba_id.
    2. Upload BOOKING_FLOW_JSON as the flow asset.
    Returns {"flow_id": ..., "validation_errors": [...]}.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1 — create flow skeleton
        create_resp = await client.post(
            f"{GRAPH_BASE}/{waba_id}/flows",
            headers=_auth_headers(),
            json={"name": "GINI Bali Booking Form", "categories": ["APPOINTMENT_BOOKING"]},
        )
        create_resp.raise_for_status()
        flow_id = create_resp.json().get("id")
        logger.info(f"Flow created: {flow_id}")

        # Step 2 — upload flow JSON asset
        flow_json_bytes = json.dumps(BOOKING_FLOW_JSON).encode("utf-8")
        asset_resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={
                "file": ("flow.json", flow_json_bytes, "application/json"),
            },
            data={
                "name": "flow.json",
                "asset_type": "FLOW_JSON",
            },
        )
        asset_data = asset_resp.json()
        logger.info(f"Flow asset upload response: {asset_data}")

        validation_errors = asset_data.get("validation_errors", [])
        return {"flow_id": flow_id, "validation_errors": validation_errors}


async def publish_booking_flow(flow_id: str) -> dict:
    """Publish a flow so it becomes PUBLISHED and usable."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/publish",
            headers=_auth_headers(),
        )
        if not resp.is_success:
            raise ValueError(f"Meta publish failed {resp.status_code}: {resp.text}")
        return resp.json()


async def get_flow_status(flow_id: str) -> dict:
    """Return id, name, status and any validation_errors for the given flow.

    Safe: catches permission errors and returns diagnostic response instead of crashing.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/{flow_id}",
            headers=_auth_headers(),
            params={"fields": "id,name,status,validation_errors,endpoint_uri"},
        )
        if not resp.is_success:
            # Catch permission errors gracefully — return diagnostic instead of 500
            if resp.status_code == 400:
                error_data = {}
                try:
                    error_data = resp.json().get("error", {})
                except:
                    pass
                error_code = error_data.get("code")
                error_msg = error_data.get("message", "Unknown error")

                if error_code == 10:  # #10 = permission denied
                    logger.warning(f"Meta permission error for flow {flow_id}: {error_msg}")
                    return {
                        "status": "error",
                        "flow_id": flow_id,
                        "error": "whatsapp_business_management permission required",
                        "meta_code": 10,
                        "meta_message": error_msg
                    }

            # Other errors: log and raise
            logger.error(f"Meta status check failed {resp.status_code}: {resp.text}")
            raise ValueError(f"Meta status check failed {resp.status_code}: {resp.text}")
        return resp.json()


# ---------------------------------------------------------------------------
# Send flow message to customer
# ---------------------------------------------------------------------------

async def send_booking_flow_message(
    sender_id: str,
    service_name: str,
    price_display: str,
    flow_id: str,
    flow_token: str,
) -> None:
    """Send an interactive Flow message to the customer to collect booking details."""
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    short_service = service_name[:60]
    body_text = (
        f"You selected *{service_name}*.\n"
        f"\U0001f4b0 *Price:* {price_display} per person\n\n"
        "Fill in the form to complete your booking."
    )
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": f"Book {short_service}"},
            "body": {"text": body_text},
            "footer": {"text": "GINI Bali \u2013 Your Bali Concierge"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": "\U0001f4cb Book Now",
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": "BOOKING"},
                },
            },
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(WA_MESSAGES_URL, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.error(f"send_booking_flow_message failed: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        logger.info(f"Flow message sent to {sender_id}: {resp.json()}")


async def create_category_flow(waba_id: str) -> dict:
    """Create and upload the 4-screen category flow under the given WABA."""
    async with httpx.AsyncClient(timeout=30) as client:
        create_resp = await client.post(
            f"{GRAPH_BASE}/{waba_id}/flows",
            headers=_auth_headers(),
            json={
                "name": "GINI Bali Order Services",
                "categories": ["APPOINTMENT_BOOKING"],
                "endpoint_uri": f"{settings.BASE_URL}/category-flow",
            },
        )
        create_data = create_resp.json()
        if create_resp.status_code not in (200, 201) or "id" not in create_data:
            logger.error(f"Category flow creation failed: {create_resp.status_code} {create_resp.text}")
            return {"error": create_data, "status_code": create_resp.status_code}
        flow_id = create_data["id"]
        logger.info(f"Category flow created: {flow_id}")

        flow_json_bytes = json.dumps(CATEGORY_FLOW_JSON).encode("utf-8")
        asset_resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={"file": ("flow.json", flow_json_bytes, "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        )
        asset_data = asset_resp.json()
        logger.info(f"Category flow asset upload: {asset_data}")
        return {"flow_id": flow_id, "validation_errors": asset_data.get("validation_errors", [])}


async def create_venue_setup_flow(waba_id: str) -> dict:
    """Create and upload the 2-screen Venue Setup flow under the given WABA.
    The flow is created with endpoint_uri = {BASE_URL}/venue-setup-flow.
    After creation, publish the flow in Meta Business Manager, then update
    VENUE_SETUP_FLOW_ID in whatsapp_flows_service.py to the new flow_id."""
    async with httpx.AsyncClient(timeout=30) as client:
        create_resp = await client.post(
            f"{GRAPH_BASE}/{waba_id}/flows",
            headers=_auth_headers(),
            json={
                "name": "GINI Bali Venue Setup",
                "categories": ["OTHER"],
                "endpoint_uri": f"{settings.BASE_URL}/venue-setup-flow",
            },
        )
        create_data = create_resp.json()
        if create_resp.status_code not in (200, 201) or "id" not in create_data:
            logger.error(f"Venue setup flow creation failed: {create_resp.status_code} {create_resp.text}")
            return {"error": create_data, "status_code": create_resp.status_code}
        flow_id = create_data["id"]
        logger.info(f"Venue setup flow created: {flow_id}")

        flow_json_bytes = json.dumps(VENUE_SETUP_FLOW_JSON).encode("utf-8")
        asset_resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={"file": ("flow.json", flow_json_bytes, "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        )
        asset_data = asset_resp.json()
        logger.info(f"Venue setup flow asset upload: {asset_data}")
        return {
            "flow_id": flow_id,
            "endpoint_uri": f"{settings.BASE_URL}/venue-setup-flow",
            "validation_errors": asset_data.get("validation_errors", []),
            "next_steps": [
                "1. Publish the flow in Meta Business Manager (WhatsApp Flows section)",
                f"2. Update VENUE_SETUP_FLOW_ID = '{flow_id}' in whatsapp_flows_service.py",
                "3. Deploy the code change",
            ]
        }


async def upload_category_flow_json(flow_id: str) -> dict:
    """Upload the current CATEGORY_FLOW_JSON to an existing flow (without creating a new one).
    Call this when the published flow schema is out of sync with the backend, then publish the flow."""
    async with httpx.AsyncClient(timeout=30) as client:
        flow_json_bytes = json.dumps(CATEGORY_FLOW_JSON).encode("utf-8")
        asset_resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={"file": ("flow.json", flow_json_bytes, "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        )
        asset_data = asset_resp.json()
        logger.info(f"Category flow JSON re-upload for {flow_id}: {asset_data}")
        validation_errors = asset_data.get("validation_errors", [])
        return {"flow_id": flow_id, "validation_errors": validation_errors, "response": asset_data}


async def upload_booking_flow_json(flow_id: str) -> dict:
    """Upload the current BOOKING_FLOW_JSON to an existing flow (without creating a new one).
    Use this to fix a schema mismatch when the Meta-published booking flow is outdated.
    After calling this, call publish_booking_flow with the same flow_id."""
    async with httpx.AsyncClient(timeout=30) as client:
        flow_json_bytes = json.dumps(BOOKING_FLOW_JSON).encode("utf-8")
        asset_resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={"file": ("flow.json", flow_json_bytes, "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        )
        asset_data = asset_resp.json()
        logger.info(f"Booking flow JSON re-upload for {flow_id}: {asset_data}")
        validation_errors = asset_data.get("validation_errors", [])
        return {"flow_id": flow_id, "validation_errors": validation_errors, "response": asset_data}


# ---------------------------------------------------------------------------
# Venue Setup Flow — 2-screen location + villa picker for unregistered guests
# ---------------------------------------------------------------------------
VENUE_SETUP_FLOW_ID = "1416814980287258"

VENUE_SETUP_FLOW_JSON = {
    "version": "7.2",
    "data_api_version": "3.0",
    "routing_model": {
        "LOCATION_SCREEN": ["VILLA_SCREEN"],
        "VILLA_SCREEN": [],
    },
    "screens": [
        {
            "id": "LOCATION_SCREEN",
            "title": "Your Location",
            "data": {
                "locations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "main-content": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "metadata": {"type": "string"},
                                },
                            },
                        },
                    },
                    "__example__": [
                        {
                            "id": "Seminyak",
                            "main-content": {"title": "Seminyak", "metadata": "Seminyak & Petitenget area"},
                            "on-click-action": {"name": "data_exchange", "payload": {"location_selection": "Seminyak"}},
                        },
                        {
                            "id": "Canggu",
                            "main-content": {"title": "Canggu", "metadata": "Canggu & Berawa area"},
                            "on-click-action": {"name": "data_exchange", "payload": {"location_selection": "Canggu"}},
                        },
                    ],
                },
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "NavigationList",
                        "name": "location_nav",
                        "label": "Select your area",
                        "list-items": "${data.locations}",
                    }
                ],
            },
        },
        {
            "id": "VILLA_SCREEN",
            "title": "Your Villa",
            "terminal": True,
            "data": {
                "villas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                        },
                    },
                    "__example__": [
                        {"id": "V1", "title": "Villa Manila"},
                        {"id": "V2", "title": "Villa Hassan"},
                    ],
                },
                "selected_location": {"type": "string", "__example__": "Seminyak"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "Form",
                        "name": "villa_form",
                        "children": [
                            {
                                "type": "RadioButtonsGroup",
                                "name": "selected_villa",
                                "label": "Select your villa",
                                "required": True,
                                "data-source": "${data.villas}",
                            },
                            {
                                "type": "Footer",
                                "label": "Continue to Services",
                                "on-click-action": {
                                    "name": "complete",
                                    "payload": {
                                        "villa_code": "${form.selected_villa}",
                                        "selected_location": "${data.selected_location}",
                                    },
                                },
                            },
                        ],
                    }
                ],
            },
        },
    ],
}


async def upload_venue_setup_flow_json(flow_id: str = VENUE_SETUP_FLOW_ID) -> dict:
    """Upload VENUE_SETUP_FLOW_JSON to the venue setup flow."""
    async with httpx.AsyncClient(timeout=30) as client:
        flow_json_bytes = json.dumps(VENUE_SETUP_FLOW_JSON).encode("utf-8")
        resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={"file": ("flow.json", flow_json_bytes, "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        )
        result = resp.json()
        logger.info(f"Venue setup flow JSON upload for {flow_id}: {result}")
        return {"flow_id": flow_id, "validation_errors": result.get("validation_errors", []), "response": result}


async def send_venue_setup_flow_message(sender_id: str, flow_token: str) -> None:
    """Send the 2-screen Venue Setup flow to collect location and villa from unregistered guests."""
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": "Tell Us Your Villa"},
            "body": {"text": "To show you services and prices for your area, please select your location and villa."},
            "footer": {"text": "GINI Bali – Your Bali Concierge"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": VENUE_SETUP_FLOW_ID,
                    "flow_cta": "Select My Villa",
                    "flow_action": "data_exchange",
                },
            },
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(WA_MESSAGES_URL, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.error(f"send_venue_setup_flow_message failed: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        logger.info(f"Venue setup flow message sent to {sender_id}: {resp.json()}")


async def send_category_flow_message(
    sender_id: str,
    flow_id: str,
    flow_token: str,
    cta: str = "Browse Services",
    header_text: str = "Order Services",
    body_text: str = "Browse and book from our full range of villa services below.",
) -> None:
    """Send the Category (Order Services) flow to a guest.

    Default args reproduce the normal browse-all path.
    Pass cta/header_text/body_text to customise the card for direct-booking shortcuts.
    """
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": header_text},
            "body": {"text": body_text},
            "footer": {"text": "GINI Bali – Your Bali Concierge"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": cta,
                    "flow_action": "data_exchange",
                },
            },
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(WA_MESSAGES_URL, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.error(f"send_category_flow_message failed: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        logger.info(f"Category flow message sent to {sender_id}: {resp.json()}")


async def send_category_booking_screen(
    sender_id: str,
    flow_id: str,
    flow_token: str,
    service_name: str,
    price_display: str,
    villa_code: str = "",
    villa_name: str = "",
) -> None:
    """Navigate directly to the BOOKING screen of the Category (Order Services) Flow.
    WCR-14c: the Category Flow already has the correct form on Meta (7 time slots
    08AM-10PM, no Number of Persons). Using it avoids needing a separate standalone
    booking flow or any Meta re-upload.
    """
    from datetime import date as _date
    min_date = _date.today().isoformat()
    short_service = service_name[:60]
    if price_display:
        body_text = (
            f"You selected *{service_name}*.\n"
            f"\U0001f4b0 *Price:* {price_display}\n\n"
            "Fill in the form to complete your booking."
        )
    else:
        body_text = f"You selected *{service_name}*.\n\nFill in the form to complete your booking."

    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": f"Book {short_service}"},
            "body": {"text": body_text},
            "footer": {"text": "GINI Bali \u2013 Your Bali Concierge"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": "\U0001f4cb Book Now",
                    "flow_action": "navigate",
                    "flow_action_payload": {
                        "screen": "BOOKING",
                    },
                },
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(WA_MESSAGES_URL, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.error(f"send_category_booking_screen failed: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        logger.info(f"Category booking screen sent to {sender_id}: {resp.json()}")


async def send_guest_registration_flow_message(
    sender_id: str,
    flow_id: str,
    flow_token: str,
    villas: list = None
) -> None:
    """Send Guest Registration Flow to new users."""
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    
    # If villas are provided, we should ideally inject them, but Flows v3 often 
    # uses a separate INIT call. For simplicity, we assume the Flow handles 
    # villa list via data_exchange or we use a simplified version.
    
    body_text = (
        "Welcome to GINI Bali! 🌴\n\n"
        "To provide you with the best experience, please register your stay details below."
    )
    
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": "Register Your Stay"},
            "body": {"text": body_text},
            "footer": {"text": "GINI Bali – Your Bali Concierge"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": flow_id,
                    "flow_cta": "📋 Register Now",
                    "flow_action": "navigate",
                    "flow_action_payload": {"screen": "REGISTRATION"},
                },
            },
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(WA_MESSAGES_URL, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.error(f"send_guest_registration_flow_message failed: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        logger.info(f"Registration Flow sent to {sender_id}: {resp.json()}")


# ---------------------------------------------------------------------------
# Discounts & Promotions Flow — 3-screen category → promo → detail
# ---------------------------------------------------------------------------

DNP_FLOW_JSON = {
    "version": "7.2",
    "data_api_version": "3.0",
    "routing_model": {
        "CATEGORIES": ["PROMOS"],
        "PROMOS": ["PROMO_DETAIL"],
        "PROMO_DETAIL": [],
    },
    "screens": [
        {
            "id": "CATEGORIES",
            "title": "Discounts & Promotions",
            "data": {
                "categories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "main-content": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "metadata": {"type": "string"},
                                },
                            },
                        },
                    },
                    "__example__": [
                        {
                            "id": "Dining",
                            "main-content": {"title": "Dining", "metadata": "Restaurant & food deals"},
                            "on-click-action": {
                                "name": "data_exchange",
                                "payload": {"category_id": "Dining"},
                            },
                        },
                    ],
                },
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "NavigationList",
                        "name": "category_nav",
                        "label": "Select a category",
                        "list-items": "${data.categories}",
                    }
                ],
            },
        },
        {
            "id": "PROMOS",
            "title": "Available Deals",
            "data": {
                "promos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "main-content": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "metadata": {"type": "string"},
                                },
                            },
                        },
                    },
                    "__example__": [
                        {
                            "id": "DP001",
                            "main-content": {"title": "10% Off Dinner", "metadata": "Exclusive discount"},
                            "on-click-action": {
                                "name": "data_exchange",
                                "payload": {"promo_id": "DP001"},
                            },
                        },
                    ],
                },
                "category_name": {"type": "string", "__example__": "Dining"},
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "NavigationList",
                        "name": "promo_nav",
                        "label": "${data.category_name}",
                        "list-items": "${data.promos}",
                    }
                ],
            },
        },
        {
            "id": "PROMO_DETAIL",
            "title": "Promotion Details",
            "terminal": True,
            "data": {
                "promo_title": {"type": "string", "__example__": "10% Off Dinner"},
                "promo_body": {
                    "type": "string",
                    "__example__": "La Favela\n\nEnjoy 10% off at La Favela.\n\nShow this message at the entrance.\n\nPromo Code: BALI10\n\nValid until: 2026-12-31",
                },
            },
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {"type": "TextHeading", "text": "${data.promo_title}"},
                    {"type": "TextBody", "text": "${data.promo_body}"},
                    {
                        "type": "Form",
                        "name": "close_form",
                        "children": [
                            {
                                "type": "Footer",
                                "label": "Got It",
                                "on-click-action": {
                                    "name": "complete",
                                    "payload": {},
                                },
                            }
                        ],
                    },
                ],
            },
        },
    ],
}


async def send_dnp_flow_message(sender_id: str, flow_token: str) -> None:
    """Send the Discounts & Promotions 3-screen flow to a guest."""
    from app.settings.config import settings as _s
    dnp_flow_id = _s.WHATSAPP_DNP_FLOW_ID
    if not dnp_flow_id:
        raise ValueError("WHATSAPP_DNP_FLOW_ID not configured — publish the DNP flow in Meta and set the env var")
    headers = {
        "Authorization": f"Bearer {settings.access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": sender_id,
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "header": {"type": "text", "text": "🎁 Discounts & Promotions"},
            "body": {"text": "Exclusive deals and offers for GINI Bali guests.\n\nBrowse available promotions below:"},
            "footer": {"text": "GINI Bali – Your Bali Concierge"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_token": flow_token,
                    "flow_id": dnp_flow_id,
                    "flow_cta": "View Deals",
                    "flow_action": "data_exchange",
                },
            },
        },
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(WA_MESSAGES_URL, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            logger.error(f"send_dnp_flow_message failed: {resp.status_code} {resp.text}")
            resp.raise_for_status()
        logger.info(f"DNP flow message sent to {sender_id}: {resp.json()}")


async def create_dnp_flow(waba_id: str) -> dict:
    """Create a new DNP flow in Meta and upload DNP_FLOW_JSON as the asset.

    Returns {"flow_id": ..., "validation_errors": [...]}.
    After creation, update WHATSAPP_DNP_FLOW_ID in Render env vars with the returned flow_id,
    then call publish_dnp_flow(flow_id) to make it live.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        create_resp = await client.post(
            f"{GRAPH_BASE}/{waba_id}/flows",
            headers=_auth_headers(),
            json={"name": "GINI Bali Discounts & Promotions", "categories": ["OTHER"]},
        )
        create_resp.raise_for_status()
        flow_id = create_resp.json().get("id")
        logger.info(f"DNP Flow created: {flow_id}")

        flow_json_bytes = json.dumps(DNP_FLOW_JSON).encode("utf-8")
        asset_resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={"file": ("flow.json", flow_json_bytes, "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        )
        asset_data = asset_resp.json()
        logger.info(f"DNP Flow asset upload response: {asset_data}")
        return {"flow_id": flow_id, "validation_errors": asset_data.get("validation_errors", [])}


async def upload_dnp_flow_json(flow_id: str) -> dict:
    """Re-upload DNP_FLOW_JSON to an existing flow (use after JSON edits)."""
    flow_json_bytes = json.dumps(DNP_FLOW_JSON).encode("utf-8")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/assets",
            headers={"Authorization": f"Bearer {settings.access_token}"},
            files={"file": ("flow.json", flow_json_bytes, "application/json")},
            data={"name": "flow.json", "asset_type": "FLOW_JSON"},
        )
        return resp.json()


async def publish_dnp_flow(flow_id: str) -> dict:
    """Publish the DNP flow so it becomes usable. Run after create_dnp_flow."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{flow_id}/publish",
            headers=_auth_headers(),
        )
        if not resp.is_success:
            raise ValueError(f"Meta DNP publish failed {resp.status_code}: {resp.text}")
        return resp.json()


# ---------------------------------------------------------------------------
# Decrypt encrypted flow response (published flows only)
# ---------------------------------------------------------------------------

def decrypt_flow_response(
    encrypted_flow_data: str,
    encrypted_aes_key: str,
    initial_vector: str,
) -> dict:
    """
    Decrypt a WhatsApp Flow response using RSA-OAEP + AES-GCM.

    Steps:
    1. Load RSA private key from settings.WHATSAPP_PRIVATE_KEY.
    2. Decrypt encrypted_aes_key with OAEP(MGF1(SHA256), SHA256).
    3. Decrypt encrypted_flow_data with AESGCM using the decrypted AES key and IV.
    4. Return parsed JSON dict.
    """
    # 1. Load private key — handle escaped newlines from env vars (\\n → \n)
    raw_key = settings.WHATSAPP_PRIVATE_KEY
    if not raw_key:
        raise ValueError(
            "WHATSAPP_PRIVATE_KEY is not set. "
            "Generate an RSA key pair, register the public key with Meta WhatsApp Manager, "
            "and add the private key as WHATSAPP_PRIVATE_KEY in Render env vars."
        )
    # Normalize: if env var stored with literal \n escape sequences, convert to real newlines
    if "\\n" in raw_key:
        raw_key = raw_key.replace("\\n", "\n")
    private_key_pem = raw_key.encode("utf-8")
    password = (
        settings.WHATSAPP_PRIVATE_KEY_PASSWORD.encode("utf-8")
        if settings.WHATSAPP_PRIVATE_KEY_PASSWORD
        else None
    )
    private_key = serialization.load_pem_private_key(private_key_pem, password=password)

    # 2. Decrypt AES key
    encrypted_aes_key_bytes = base64.b64decode(encrypted_aes_key)
    aes_key = private_key.decrypt(
        encrypted_aes_key_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    # 3. Decrypt flow data
    iv = base64.b64decode(initial_vector)
    ciphertext = base64.b64decode(encrypted_flow_data)
    aesgcm = AESGCM(aes_key)
    plaintext = aesgcm.decrypt(iv, ciphertext, None)

    # 4. Parse and return
    return json.loads(plaintext.decode("utf-8"))
