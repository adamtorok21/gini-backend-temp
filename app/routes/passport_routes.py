from fastapi import APIRouter
router = APIRouter(prefix="/passports", tags=["passport_routes"])
@router.get("/notifications/{guest_id}")
async def notifications(guest_id: str): return {"notifications": []}
@router.post("/upload")
async def upload(): return {"success": True, "message": "Received (dev mode)."}
