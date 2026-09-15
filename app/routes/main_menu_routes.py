# RECONSTRUCTED (sandbox) — menus + sheet-nav, wired to live Google Sheet CMS.
from fastapi import APIRouter, Query
import re
from app.services import menu_services

router = APIRouter(prefix="/menu", tags=["main_menu_routes"])

def _norm(rows, key):
    return [(r if isinstance(r, dict) else {key: r}) for r in (rows or [])]

def _nav(rows, key):
    out = []
    for r in (rows or []):
        if isinstance(r, dict):
            name = r.get(key) or r.get(key.capitalize()) or r.get("name") or r.get("title")
            ep = r.get("endpoint") or r.get("Endpoint") or ""
            out.append({key: name, "endpoint": ep})
        else:
            out.append({key: r, "endpoint": ""})
    return out

@router.get("/villas")
async def villas():
    try: return {"villas": await menu_services.get_all_villas()}
    except Exception as e: return {"villas": [], "error": str(e)}

@router.get("/categories")
async def categories(location_zone: str = Query(None), villa_code: str = Query(None)):
    try: return {"categories": await menu_services.get_categories_only(location_zone, villa_code)}
    except Exception as e: return {"categories": [], "error": str(e)}

@router.get("/zones")
async def zones():
    try:
        z = await menu_services.get_available_zones()
        return {"zones": z, "data": _norm(z, "zone")}
    except Exception as e: return {"zones": [], "data": [], "error": str(e)}

def _catnav(rows):
    out = []
    for r in (rows or []):
        if isinstance(r, dict):
            out.append({
                "category": r.get("category") or r.get("Category") or r.get("name") or r.get("Item") or r.get("title"),
                "endpoint": r.get("endpoint") or r.get("Endpoint") or "",
                "description": r.get("description") or r.get("Description") or r.get("WA Description") or "",
                "picture": r.get("picture") or r.get("Picture") or r.get("image") or "",
                "button": r.get("button") or r.get("Caption Button") or "See Options",
            })
        else:
            out.append({"category": r, "endpoint": "", "description": "", "picture": "", "button": "See Options"})
    return out

_SHEET_MENUS = {"recommendations", "bali handbook"}
_DNP_MENUS = {"discount & promotions", "discounts & promotions"}

async def _sub(category, location_zone, villa_code):
    cl = category.strip().lower()
    if cl == "order services":
        cats = await menu_services.get_categories_only(location_zone, villa_code)
        names = [ (c.get("category") or c.get("Category") or c.get("title") or c.get("name")) if isinstance(c, dict) else c for c in cats ]
        return {"data": [{"category": n} for n in names if n]}
    if cl in _SHEET_MENUS:
        return {"data": _catnav(await menu_services.get_sheet_menu_categories(category))}
    if cl in _DNP_MENUS:
        return {"data": _catnav(await menu_services.get_dnp_categories())}
    subs = await menu_services.get_sub_category(category, location_zone, villa_code)
    return {"data": _norm(subs, "subcategory")}

@router.get("/sub-category/{category:path}")
async def sub_category(category: str, location_zone: str = Query(None), villa_code: str = Query(None), language: str = Query(None)):
    try: return await _sub(category, location_zone, villa_code)
    except Exception as e: return {"data": [], "error": str(e)}

@router.get("/sub/{category:path}")
async def sub_alias(category: str, location_zone: str = Query(None), villa_code: str = Query(None), language: str = Query(None)):
    try: return await _sub(category, location_zone, villa_code)
    except Exception as e: return {"data": [], "error": str(e)}

@router.get("/service-items/{subcategory:path}")
async def service_items(subcategory: str, location_zone: str = Query(None), villa_code: str = Query(None), language: str = Query(None)):
    try:
        items = await menu_services.get_service_items(subcategory, villa_code, location_zone) or []
        for it in items:
            if isinstance(it, dict) and it.get("button"):
                digits = re.sub(r"[^\d]", "", str(it["button"]))
                if digits:
                    it["button"] = digits  # "10.000" (IDR thousands) -> "10000"
        return {"data": items}
    except Exception as e: return {"data": [], "error": str(e)}

@router.get("/price_distribution")
async def price_distribution(service_item: str = Query(None), location_zone: str = Query(None), villa_code: str = Query(None)):
    # Frontend wants total_customer_price for a service in the guest's location.
    try:
        if service_item:
            # get_location_specific_price expects a villa_code; a real villa maps to a zone.
            # If only a zone is given, base price is fine for the sandbox demo.
            price_str = await menu_services.get_service_base_price(service_item)
            if villa_code:
                try: price_str = await menu_services.get_location_specific_price(service_item, villa_code)
                except Exception: pass
            digits = re.sub(r"[^\d]", "", str(price_str or ""))
            total = int(digits) if digits else 0
            return {"success": True, "total_customer_price": total, "service_item": service_item}
        return {"success": True, "data": await menu_services.get_price_distribution()}
    except Exception as e:
        return {"success": False, "total_customer_price": 0, "error": str(e)}

# ---- sheet-nav: Recommendations / Bali Handbook ----
@router.get("/sheet-nav/categories/{main_menu:path}")
async def sn_categories(main_menu: str):
    try: return {"categories": _nav(await menu_services.get_sheet_menu_categories(main_menu), "category")}
    except Exception as e: return {"categories": [], "error": str(e)}

@router.get("/sheet-nav/subcategories/{main_menu}/{category:path}")
async def sn_subcategories(main_menu: str, category: str):
    try: return {"subcategories": _nav(await menu_services.get_sheet_menu_subcategories(main_menu, category), "subcategory")}
    except Exception as e: return {"subcategories": [], "error": str(e)}

@router.get("/sheet-nav/sub-subcategories/{main_menu}/{category}/{subcategory:path}")
async def sn_subsub(main_menu: str, category: str, subcategory: str):
    try: return {"sub_subcategories": _nav(await menu_services.get_sheet_menu_sub_subcategories(main_menu, category, subcategory), "sub_subcategory")}
    except Exception as e: return {"sub_subcategories": [], "error": str(e)}
