# RECONSTRUCTED (sandbox) — AI concierge: RAG (Pinecone) + live catalog + villa + memory.
import time, base64, re, asyncio
import httpx
from fastapi import APIRouter, Body, Query
from app.settings.config import settings
from app.services import menu_services
from app.services.rag_service import rag_service

router = APIRouter(prefix="/chatbot", tags=["chatbot_routes"])
_ORDERS = {}
_HISTORY = {}

def _amount(v):
    d = re.sub(r"[^\d]", "", str(v or "")); return int(d) if d else 0

async def _create_xendit_invoice(order_number, service_name, amount):
    auth = base64.b64encode(f"{settings.XENDIT_SECRET_KEY}:".encode()).decode()
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post("https://api.xendit.co/v2/invoices", headers={"Authorization": f"Basic {auth}"},
            json={"external_id": order_number, "amount": amount, "currency": "IDR",
                  "description": service_name, "success_redirect_url": "https://ginibali.com/order-confirmed"})
    j = r.json(); return j.get("invoice_url"), j.get("id")

async def _simulate_sp_accept(order_number, service_name, amount):
    await asyncio.sleep(6)
    o = _ORDERS.get(order_number)
    if not o or o.get("status") != "Awaiting Provider": return
    o["status"] = "Awaiting Payment"; o["sp_accepted"] = True
    if settings.XENDIT_SECRET_KEY and amount > 0:
        try:
            url, iid = await _create_xendit_invoice(order_number, service_name, amount)
            o["payment_url"] = url; o["invoice_id"] = iid; o["payment_status"] = "pending"
        except Exception as e: o["error"] = str(e)

@router.get("/orders/notifications/{guest_id}")
async def order_notifications(guest_id: str): return {"notifications": []}

@router.post("/generate-response")
async def generate_response(payload: dict = Body(default={}), user_id: str = Query("web")):
    msg = payload.get("query") or payload.get("message") or payload.get("text") or ""
    villa = payload.get("villa_code"); zone = payload.get("location_zone")
    chat_type = payload.get("chat_type") or "general"
    if not settings.OPENAI_API_KEY:
        return {"success": True, "response": "AI is not configured yet."}
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        # ---- OUR DATA: RAG (Pinecone) + live service catalog + this villa ----
        try: rag_ctx = await rag_service.get_rag_context(msg, chat_type=chat_type, villa_code=villa or "WEB_VILLA_01")
        except Exception: rag_ctx = ""
        try: catalog = (await menu_services.get_service_catalog_context(villa, zone) or "")[:5000]
        except Exception: catalog = ""
        villa_line = ""
        try:
            if villa:
                vi = await menu_services.get_villa_info_by_code(villa)
                if isinstance(vi, dict):
                    villa_line = f"\nGUEST'S VILLA: {vi.get('name') or villa} in {vi.get('location') or 'Bali'}."
        except Exception: pass
        system = (
            "You are Gini, the friendly AI concierge for GINI Bali, helping a guest during their villa stay in Bali. "
            "Be warm, natural, concise and genuinely helpful, like a knowledgeable local friend. "
            "You have real memory of this conversation, so follow the thread and never reset with a generic greeting. "
            "PRIORITISE the info under 'OUR DATA' below (curated by GINI Bali) when answering; only use your own general "
            "Bali knowledge if OUR DATA doesn't cover it. For BOOKABLE SERVICES, only offer what's in the SERVICE CATALOG "
            "(with its real prices). Give specific, useful answers; only point to WhatsApp when a human is truly needed. "
            "Keep replies short and easy to read." + villa_line
            + ("\n\n--- OUR DATA (use first) ---\n" + rag_ctx if rag_ctx else "")
            + "\n\nSERVICE CATALOG:\n" + catalog
        )
        hist = _HISTORY.setdefault(user_id, [])
        messages = [{"role": "system", "content": system}] + hist[-8:] + [{"role": "user", "content": msg}]
        r = await client.chat.completions.create(
            model=settings.OPENAI_MODEL_NAME or "gpt-4o-mini", messages=messages, max_tokens=450, temperature=0.7)
        reply = r.choices[0].message.content
        hist.append({"role": "user", "content": msg}); hist.append({"role": "assistant", "content": reply})
        if len(hist) > 16: _HISTORY[user_id] = hist[-16:]
        return {"success": True, "response": reply, "used_our_data": bool(rag_ctx)}
    except Exception as e:
        return {"success": False, "response": "Sorry, I couldn't process that right now.", "error": str(e)}

@router.post("/create-booking-payment")
async def create_booking_payment(payload: dict = Body(default={})):
    service_name = payload.get("service_item") or payload.get("title") or payload.get("service_name") or "Service"
    amount = _amount(payload.get("price") or payload.get("button") or payload.get("amount"))
    order_number = f"GINI-{int(time.time())}"
    _ORDERS[order_number] = {"status": "Awaiting Provider", "service_name": service_name, "amount": amount,
                             "payment_url": None, "payment_status": "none", "villa_code": payload.get("villa_code")}
    asyncio.create_task(_simulate_sp_accept(order_number, service_name, amount))
    return {"success": True, "order_number": order_number, "status": "Awaiting Provider",
            "message": "Booking request sent to service providers. Waiting for one to accept..."}

@router.get("/booking-status/{order_number}")
async def booking_status(order_number: str, user_id: str = ""):
    o = _ORDERS.get(order_number)
    if not o: return {"status": "UNKNOWN", "payment_status": "none", "order_number": order_number}
    return {"order_number": order_number, **o}
