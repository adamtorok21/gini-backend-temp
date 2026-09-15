# RECONSTRUCTED (sandbox) dev scaffold — admin user management.
from fastapi import APIRouter, Request
router = APIRouter(prefix="/admin", tags=["admin_users"])
def ok(**kw):
    d = {"success": True}; d.update(kw); return d
@router.get("/users")
async def users():
    return ok(users=[])
@router.post("/users/create")
async def create_user(request: Request):
    return ok(message="User created (dev mode).")
@router.put("/users/{id}")
async def update_user(id: str, request: Request):
    return ok()
@router.delete("/users/{id}")
async def delete_user(id: str):
    return ok()
@router.post("/users/{id}/reset-password")
async def reset_pw(id: str):
    return ok()
@router.post("/users/{id}/deactivate")
async def deactivate(id: str):
    return ok()
@router.get("/villas")
async def admin_villas():
    return ok(villas=[])
