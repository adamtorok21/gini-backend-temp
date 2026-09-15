from app.db.session import order_collection, db, villa_code_collection
from pymongo import ReturnDocument
from datetime import datetime
from app.models.order_summary import Order
from typing import Dict, Optional


active_chat_sessions: Dict[str, Order] = {}
order_sessions: Dict[str, str] = {}


async def get_user_villa_code(sender_id: str):
    try:
        document = await villa_code_collection.find_one({"sender_id": sender_id})    
        if document:
            return document.get("villa_code")
        else:
            return None
    except Exception as e:
        print(f"Error retrieving villa code: {e}")
        return None


async def get_active_order_context(sender_id: str) -> str:
    """Return an AI-context string describing the guest's latest PENDING order,
    so a guest asking "any update?" gets the correct status instead of a generic
    reply. Returns "" when there is no pending order. Never raises — a failure
    must not block the AI response.
    """
    try:
        order = await order_collection.find_one(
            {"sender_id": sender_id,
             "$or": [
                 {"booking_status": {"$in": ["AWAITING_SP_CONFIRMATION", "AWAITING_PAYMENT"]}},
                 {"status": {"$in": ["requested", "pending", "accepted"]}},
             ]},
            sort=[("created_at", -1)],
        )
        if not order:
            return ""
        on = order.get("order_number", "your order")
        svc = order.get("service_name", "your service")
        bs = order.get("booking_status") or order.get("status") or ""
        if bs in ("AWAITING_PAYMENT", "accepted"):
            return (
                f"\n- ACTIVE ORDER: {on} ({svc}) has been ACCEPTED by the service "
                f"provider and is awaiting payment. If the guest asks for an update, "
                f"tell them their order is accepted and to complete payment via the "
                f"secure link already sent in this chat."
            )
        return (
            f"\n- ACTIVE ORDER: {on} ({svc}) is placed and WAITING FOR THE SERVICE "
            f"PROVIDER TO ACCEPT it. If the guest asks for any update or status on "
            f"their order, reply EXACTLY: \"Your order {on} is placed and waiting for "
            f"the service provider to accept it. As soon as they do, your secure "
            f"payment link will appear right here. Thanks for your patience!\""
        )
    except Exception:
        return ""


async def get_next_order_id() -> str:
    counter_doc = await db.counters.find_one_and_update(
        {"_id": "order_number"},
        {"$inc": {"sequence_value": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    return f"EB{counter_doc['sequence_value']:0d}"


async def initiate_chat_session(sender_id: str, service_name: str, person_count: str, base_price: str, date: datetime = None, time: str = None) -> Order:
    order_number = await get_next_order_id()
    
    user_villa_code = await get_user_villa_code(sender_id)
   
    try:
        persons = int(person_count)
        unit_price = int(base_price.replace(",", "").replace(" ", ""))
        total_price = unit_price * persons
        formatted_price = f"{total_price:,}"
    except (ValueError, AttributeError):
        formatted_price = base_price

    new_order = Order(
        order_number=order_number,
        service_name=service_name,
        sender_id=sender_id,
        date=date,
        time=time,
        price=formatted_price,
        confirmation=False,
        villa_code=user_villa_code,
        status="pending",
    )
    order_sessions[sender_id] = new_order.order_number
    return new_order


import logging

logger = logging.getLogger(__name__)

async def save_order_to_db(order: dict):
    # Resolve and store guest_id at write time so all collections share a canonical identity
    if not order.get("guest_id"):
        try:
            from app.db.session import guest_profile_collection as _gpc
            import re as _re
            _sid = order.get("sender_id", "")
            _phone = _re.sub(r"[\s\-\(\)\.\+]", "", _sid)
            _gp = await _gpc.find_one({"phone_number": _phone})
            if _gp:
                order["guest_id"] = _gp.get("guest_id")
        except Exception:
            pass
    logger.info(f"Booking flow stage: Order {order.get('order_number')} created in MongoDB with status 'pending'")
    await order_collection.insert_one(order)


def format_order_summary(order: dict) -> str:
    # Prefer booking_date (plain string from web bookings) over date (datetime from WA flow)
    order_date = order.get("booking_date") or order.get("date")
    if order_date and isinstance(order_date, datetime):
        order_date = order_date.strftime("%Y-%m-%d")
    elif isinstance(order_date, str):
        order_date = order_date
    else:
        order_date = "N/A"

    customer_display = (
        order.get('customer_name') or
        order.get('customer_id') or
        order.get('sender_id', 'N/A')
    )
    summary_lines = [
        f"Customer: {customer_display}",
        f"Order ID: {order.get('order_number', 'N/A')}",
        f"Service: {order.get('service_name', 'N/A')}",
        f"Date: {order_date}",
        f"Time: {order.get('time', 'N/A')}",
        f"Persons: {order.get('persons', 'N/A')}",
        f"Price: {order.get('price', 'N/A')}",
        f"Location: {order.get('villa_code', 'N/A')}",
    ]
    return "\n".join(summary_lines)



async def check_order_confirmation(order_number: str) -> bool:
    order_doc: Optional[dict] = await order_collection.find_one({"order_number": order_number})
    if order_doc is not None:
        return bool(order_doc.get("confirmation"))
    return False
    

async def update_order_confirmation(order_number: str, confirmation: bool) -> str:
    updated_order = await order_collection.find_one_and_update(
        {"order_number": order_number},
        {"$set": {"confirmation": confirmation}},
        return_document=ReturnDocument.AFTER
    )
    if updated_order:
        return updated_order.get("sender_id")
    return None


async def get_sender_id_by_order(order_number: str) -> Optional[str]:
    order = await order_collection.find_one({"order_number": order_number})
    if order:
        return order.get("sender_id")
    return None


async def get_order_by_number(order_number: str) -> Optional[dict]:
    try:
        order_doc = await order_collection.find_one({"order_number": order_number})
        
        if not order_doc:
            return None
        return {
            "order_number": order_doc.get("order_number"),
            "service_name": order_doc.get("service_name"),
            "date": order_doc.get("date").strftime("%d-%m-%Y") if order_doc.get("date") else "N/A",
            "time": order_doc.get("time"),
            "price": order_doc.get("price"),
            "sender_id": order_doc.get("sender_id"),
            "status": order_doc.get("status")
        }
        
    except Exception as e:
        print(f"Error fetching order {order_number}: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# CONTAINERIZED: Booking Cancellation Flow
# ──────────────────────────────────────────────────────────────
async def cancel_order(order_number: str, reason: str = "User cancelled") -> dict:
    """Cancel a booking if it hasn't been paid yet."""
    try:
        from app.services.status_service import BookingStatusManager
        from app.models.order_summary import BookingStatus, PaymentStatus
        
        # 1. Fetch current order
        order_doc = await order_collection.find_one({"order_number": order_number})
        if not order_doc:
            return {"success": False, "error": f"Order {order_number} not found"}
        
        # 2. Check payment status truth
        if order_doc.get("fsm_payment_status") == PaymentStatus.PAID:
            return {"success": False, "error": "Cannot cancel a paid order. Contact support for refunds."}

        # 3. Use FSM for atomic transition
        res = await BookingStatusManager.transition_booking_status(
            order_number=order_number,
            target_status=BookingStatus.CANCELLED,
            changed_by="USER_ACTION",
            reason=reason
        )
        
        if not res["success"]:
            return res
            
        # 4. Optional: keep legacy cancellation fields for now
        await order_collection.update_one(
            {"order_number": order_number},
            {"$set": {
                "cancellation_reason": reason,
                "cancelled_at": datetime.now()
            }}
        )
        
        logger.info(f"Booking flow stage: Order {order_number} cancelled via FSM. Reason: {reason}")
        return {"success": True, "message": f"Order {order_number} has been cancelled."}
    except Exception as e:
        logger.error(f"Error cancelling order {order_number}: {e}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"Error cancelling order {order_number}: {e}")
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# CONTAINERIZED: Booking History Endpoint
# ──────────────────────────────────────────────────────────────
async def get_booking_history(sender_id: str, page: int = 1, limit: int = 20) -> dict:
    """Get paginated booking history for a user."""
    try:
        skip = (page - 1) * limit
        total = await order_collection.count_documents({"sender_id": sender_id})
        
        cursor = order_collection.find({"sender_id": sender_id}).sort("created_at", -1).skip(skip).limit(limit)
        orders = await cursor.to_list(length=limit)
        
        history = []
        for order in orders:
            order_date = order.get("date")
            if order_date and isinstance(order_date, datetime):
                order_date = order_date.strftime("%d-%m-%Y")
            elif not isinstance(order_date, str):
                order_date = "N/A"
            
            history.append({
                "order_number": order.get("order_number"),
                "service_name": order.get("service_name"),
                "date": order_date,
                "time": order.get("time", "N/A"),
                "price": order.get("price", "N/A"),
                "status": order.get("status", "pending"),
                "villa_code": order.get("villa_code", "N/A"),
                "created_at": order.get("created_at").isoformat() if order.get("created_at") else None,
                "cancellation_reason": order.get("cancellation_reason")
            })
        
        return {
            "success": True,
            "total": total,
            "page": page,
            "limit": limit,
            "bookings": history
        }
    except Exception as e:
        logger.error(f"Error fetching booking history for {sender_id}: {e}")
        return {"success": False, "error": str(e), "bookings": []}

