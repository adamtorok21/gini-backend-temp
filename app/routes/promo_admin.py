# RECONSTRUCTED (sandbox) dev scaffold — promo management.
from fastapi import APIRouter, Request
router = APIRouter(prefix="/promos", tags=["promo_admin"])
def ok(**kw):
    d = {"success": True}; d.update(kw); return d
@router.get("/list")
async def promos_list():
    return ok(promos=[])
@router.get("")
async def promos():
    return ok(promos=[])
@router.post("/create")
async def create_promo(request: Request):
    return ok(message="Promo created (dev mode).")
