from fastapi import APIRouter, Body
from app.services import currency_convertor
router = APIRouter(prefix="/currency", tags=["currency_route"])
@router.post("")
async def convert(payload: dict = Body(default={})):
    try:
        q = payload.get("query") or payload.get("message") or ""
        uid = payload.get("user_id") or "web"
        lang = payload.get("language") or "EN"
        return await currency_convertor.currency_ai(uid, q, lang)
    except Exception as e:
        return {"response": "Currency service needs a valid AI key.", "error": str(e)}
