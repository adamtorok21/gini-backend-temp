from fastapi import APIRouter, Query
from app.services import menu_services
router = APIRouter(prefix="/dnp", tags=["dnp_routes"])
@router.get("/categories")
async def cats():
    try: return {"categories": await menu_services.get_dnp_categories()}  # list[str]
    except Exception as e: return {"categories": [], "error": str(e)}
@router.get("/subcategories")
async def subs(category: str = Query(None)):
    try: return {"subcategories": await menu_services.get_dnp_subcategories(category)}  # list[promo dict]
    except Exception as e: return {"subcategories": [], "error": str(e)}
@router.get("/promo")
async def promo(promo_id: str = Query(None), id: str = Query(None)):
    try: return {"promo": await menu_services.get_dnp_promo(promo_id or id)}
    except Exception as e: return {"promo": None, "error": str(e)}
