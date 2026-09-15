# RECONSTRUCTED (sandbox) dev scaffold — disbursement admin controls.
# Mounted under /dashboard-api in main.py, so paths become /dashboard-api/disbursement/...
from fastapi import APIRouter, Request
router = APIRouter(tags=["admin_control_routes"])
def ok(**kw):
    d = {"success": True}; d.update(kw); return d
@router.post("/disbursement/{order_number}/hold")
async def hold(order_number: str):
    return ok(message="Disbursement held (dev mode).")
@router.post("/disbursement/{order_number}/resume")
async def resume(order_number: str):
    return ok(message="Disbursement resumed (dev mode).")
@router.post("/disbursement/{order_number}/cancel")
async def cancel(order_number: str):
    return ok(message="Disbursement cancelled (dev mode).")
