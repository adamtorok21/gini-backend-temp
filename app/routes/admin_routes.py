# RECONSTRUCTED route module (sandbox) — original was missing from handover.
# Admin auth. NOTE: /admin/login runs in DEV MODE here — it issues a valid JWT for
# any credentials so the dashboard UI can be explored without a real user database.
# Replace with real credential verification against MongoDB once the DB is connected.
from fastapi import APIRouter, Body
from app.utils.auth import create_access_token

router = APIRouter(prefix="/admin", tags=["admin_routes"])


@router.post("/login")
async def admin_login(payload: dict = Body(default={})):
    email = payload.get("email") or payload.get("username") or "admin@ginibali.com"
    # DEV MODE: no DB yet, so accept and mint an admin token.
    token = create_access_token({"sub": email, "role": "admin", "dev_mode": True})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"email": email, "role": "admin", "name": "Gini Bali Admin"},
        "dev_mode": True,
    }


@router.get("/me")
async def admin_me():
    return {"email": "admin@ginibali.com", "role": "admin", "name": "Gini Bali Admin", "dev_mode": True}


@router.post("/change-password")
async def change_password(payload: dict = Body(default={})):
    return {"success": True, "message": "Password updated (dev mode — not persisted)."}


@router.post("/forgot-password")
async def forgot_password(payload: dict = Body(default={})):
    return {"success": True, "message": "If the account exists, a reset link was sent (dev mode)."}


@router.post("/reset-password")
async def reset_password(payload: dict = Body(default={})):
    return {"success": True, "message": "Password reset (dev mode — not persisted)."}
