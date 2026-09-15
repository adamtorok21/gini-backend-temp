from fastapi import APIRouter, Query
from app.services import menu_services
router = APIRouter(prefix="/dnp", tags=["dnp_routes"])
def _nav(rows, key):
    out=[]
    for r in (rows or []):
        if isinstance(r,dict): out.append({key: r.get(key) or r.get(key.capitalize()) or r.get("name"), "endpoint": r.get("endpoint") or ""})
        else: out.append({key:r,"endpoint":""})
    return out
@router.get("/categories")
async def cats():
    try: return {"categories": _nav(await menu_services.get_dnp_categories(), "category")}
    except Exception as e: return {"categories": [], "error": str(e)}
@router.get("/subcategories")
async def subs(category: str = Query(None)):
    try: return {"subcategories": _nav(await menu_services.get_dnp_subcategories(category), "subcategory")}
    except Exception as e: return {"subcategories": [], "error": str(e)}
@router.get("/promo")
async def promo(promo_id: str = Query(None), id: str = Query(None)):
    try: return {"promo": await menu_services.get_dnp_promo(promo_id or id)}
    except Exception as e: return {"promo": None, "error": str(e)}
