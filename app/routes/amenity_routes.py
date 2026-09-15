from fastapi import APIRouter, Request
router = APIRouter(prefix="/amenities", tags=["amenity_routes"])
def ok(**kw):
    d={"success":True}; d.update(kw); return d
_ITEMS=["Toilet Paper","Tissues","Shampoo","Conditioner","Body Wash","Hand Soap","Towels","Pool Towels","Bed Linen","Pillows","Blankets","Slippers","Drinking Water","Coffee & Tea Refill","Sugar / Sweetener","Trash Bags","Cleaning Service","Extra Bed Setup"]
@router.get("/notifications/{guest_id}")
async def notifications(guest_id: str): return {"notifications": []}
@router.get("")
async def amenities(request: Request): return ok(amenities=[], requests=[])
@router.get("/items")
async def items(request: Request): return ok(items=_ITEMS)
@router.post("/request")
async def request_amenity(request: Request):
    return ok(message="Amenity request submitted. The villa team has been notified.")
@router.get("/stats/summary")
async def stats(): return ok(summary={"open":0,"in_progress":0,"resolved":0})
@router.patch("/{id}/status")
async def status(id: str, request: Request): return ok()
