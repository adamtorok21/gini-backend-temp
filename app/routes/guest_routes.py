import time, random
from fastapi import APIRouter, Body
from app.services import menu_services
router = APIRouter(prefix="/guest", tags=["guest_routes"])
@router.get("/profile/{phone}")
async def profile(phone: str): return {"success": True, "profile": {}}
@router.post("/register")
async def register(payload: dict = Body(default={})):
    gid = f"guest_{int(time.time())}_{random.randint(1000,9999)}"
    return {"success": True, "guest_id": gid, "message": "Registered (dev mode — not persisted without DB)."}
@router.post("/villa-context")
async def villa_context(payload: dict = Body(default={})):
    code = payload.get("villa_code") or payload.get("villaCode")
    try:
        info = await menu_services.get_villa_info_by_code(code) if code else None
        return {"success": bool(info), "villa": info or {}, "location_zone": (info or {}).get("location") if isinstance(info, dict) else None}
    except Exception as e:
        return {"success": False, "villa": {}, "error": str(e)}
@router.get("/journey-link/{villa}")
async def journey_link(villa: str):
    return {"success": True, "link": f"https://ginibali.com/guest-journey?villa={villa}"}
