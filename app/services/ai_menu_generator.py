# app/services/ai_menu_generator.py
from typing import Dict, List, Optional, Any
import re
import json
from app.services.menu_services import cache
from app.services.openai_client import client

class AIMenuGenerator:
    """Generate dynamic WhatsApp menus based on AI-detected intent using Google Sheets data"""
    
    def __init__(self):
        # ═══════════════════════════════════════════════════════════════
        # SUBCATEGORY IMAGE URLS - Maps subcategory to S3 image URL
        # ═══════════════════════════════════════════════════════════════
        self.subcategory_image_urls = {
            "Airport Pickup/Drop": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Airport_Pickup_Drop.jpg",
            "Boxing": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Boxing.jpg",
            "Equipment Rental": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Equipment_Rental.jpg",
            "Fast Boat": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Fast_Boat.jpg",
            "Flowers": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Flowers.jpg",
            "Helicopter Ride": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Helicopter_Ride.jpg",
            "IV Drip": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/IV_Drip.jpg",
            "In-House Dining": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/In_House_Dining.jpg",
            "Island Tour": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Island_Tour.jpg",
            "Kickboxing": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Kickboxing.jpg",
            "Laundry": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Laundry.jpg",
            "Massage": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Massage.jpg",
            "Movie Night": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Movie_Night.jpg",
            "Muay Thai": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Muay_Thai.jpg",
            "Photography": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Photography.jpg",
            "Physiotherapy": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Physiotherapy.jpg",
            "Private Barber": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Private_Barber.jpg",
            "Private Chef": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Private_Chef.jpg",
            "Shisha Rental": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Shisha_Rental.jpg",
            "Tour with Driver": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Tour_with_Driver.jpg",
            "Yoga": "https://easybali.s3.ap-southeast-2.amazonaws.com/services/Yoga.jpg"
        }
        
        self.service_categories = {
            "massage": {
                "category": "Health & Wellness",
                "subcategory": "Massage",
                "keywords": ["massage", "spa", "balinese", "aroma", "aromatherapy", "shiatsu", "reflexology", "facial", "body treatment"]
            },
            "yoga": {
                "category": "Health & Wellness",
                "subcategory": "Yoga",
                "keywords": ["yoga", "meditation", "mobility", "stretch", "pilates"]
            },
            "muay_thai": {
                "category": "Health & Wellness",
                "subcategory": "Muay Thai",
                "keywords": ["muay thai", "martial arts", "sparring", "thai boxing"]
            },
            "boxing": {
                "category": "Health & Wellness",
                "subcategory": "Boxing",
                "keywords": ["boxing", "fight", "punch"]
            },
            "kickboxing": {
                "category": "Health & Wellness",
                "subcategory": "Kickboxing",
                "keywords": ["kickboxing", "kick"]
            },
            "photography": {
                "category": "Services",
                "subcategory": "Photography",
                "keywords": ["photography", "photoshoot", "photographer", "photo", "camera", "video"]
            },
            "private_chef": {
                "category": "Dining",
                "subcategory": "Private Chef",
                "keywords": ["chef", "cook", "private chef", "bbq", "dinner"]
            },
            "iv_drip": {
                "category": "Health & Wellness",
                "subcategory": "IV Drip",
                # RECON-KW-01 (2026-08-26): "physiotherapy" removed — it was
                # misrouting guests asking about physiotherapy into the IV Drip
                # subcategory instead of the real, dedicated "Physiotherapy"
                # subcategory (confirmed live in the catalog — see
                # subcategory_image_urls above). No corrected dedicated entry
                # was added here because its live-sheet Category value could
                # not be confirmed from this codebase (unlike Island Tour
                # below, verified via test_menu_api.py's live integration
                # test) — guessing wrong risks a silent no-op. A guest asking
                # about physiotherapy now falls through to the AI classifier
                # (intelligent_service_check) with no deterministic backstop,
                # rather than being actively misrouted to the wrong service.
                "keywords": ["iv drip", "medical", "doctor", "vitamin", "hangover"]
            },
            "tour_driver": {
                "category": "Transportation",
                "subcategory": "Tour with Driver",
                # RECON-KW-01: "island tour" removed from here — it now has
                # its own dedicated entry below, confirmed via a live
                # integration test to be a real, separate Transportation
                # subcategory (test_menu_api.py TC-MA-02), not a synonym for
                # Tour with Driver.
                "keywords": ["driver", "tour", "car", "van", "transport", "driver with car"]
            },
            # RECON-KW-01: added — confirmed via a live integration test
            # (tests/integration/test_menu_api.py TC-MA-02, GET
            # /menu/sub-category/Transportation) that "Island Tour" is a real,
            # separate Transportation subcategory alongside Tour with Driver
            # and Fast Boat. Previously only reachable via tour_driver's
            # "island tour" keyword, which routed to the wrong subcategory
            # (Tour with Driver) for booking/pricing purposes.
            "island_tour": {
                "category": "Transportation",
                "subcategory": "Island Tour",
                "keywords": ["island tour", "island hopping", "island trip"]
            },
            "helicopter": {
                "category": "Transportation",
                "subcategory": "Helicopter Ride",
                "keywords": ["helicopter", "heli", "chopper", "flight"]
            },
            "airport_transfer": {
                "category": "Transportation",
                "subcategory": "Airport Pickup/Drop",
                "keywords": ["airport", "pickup", "drop", "transfer", "arrival", "departure"]
            },
            "bike_rental": {
                "category": "Rental",
                "subcategory": "Bike Rental",
                "keywords": ["scooter", "bike", "nmax", "pcx", "scoopy", "motorcycle", "yamaha", "honda"]
            },
            "fast_boat": {
                "category": "Transportation",
                "subcategory": "Fast Boat",
                "keywords": ["boat", "fast boat", "gili", "nusa penida", "lembongan", "ferry"]
            },
            "flowers": {
                "category": "Services",
                "subcategory": "Flowers",
                "keywords": ["flowers", "bouquet", "floral", "anniversary", "birthday"]
            },
            "laundry": {
                "category": "Services",
                "subcategory": "Laundry",
                "keywords": ["laundry", "wash", "dry clean", "ironing"]
            },
            "language_lesson": {
                "category": "Services",
                "subcategory": "Local Language Lesson",
                "keywords": ["language", "indonesian", "bahasa", "balinese", "lesson", "cultural", "conversation"]
            },
            # WCR-12: shisha was absent → detect_service_intent returned None
            # → intelligent_service_check excluded it when cache was cold
            # → we_offer_it=False → WCR-10 fallback never fired → no menu sent.
            "shisha": {
                "category": "Villa Experiences",
                "subcategory": "Shisha Rental",
                "keywords": ["shisha", "hookah", "waterpipe", "narghile", "shisha party", "hookah party"]
            }
        }
        self._service_check_cache: Dict[str, Any] = {}

    async def intelligent_service_check(self, query: str) -> Dict[str, Any]:
        """Use AI to determine if user is requesting/discussing a specific service we offer."""
        _cache_key = query.lower().strip()[:120]
        if _cache_key in self._service_check_cache:
            return self._service_check_cache[_cache_key]
        if len(self._service_check_cache) >= 200:
            self._service_check_cache.clear()

        our_services = []
        try:
            main_df = cache.get("main_menu_design")
            design_df = cache.get("design_df")
            
            if main_df is not None and not main_df.empty and design_df is not None and not design_df.empty:
                if "Menu Location" in main_df.columns and "Category" in design_df.columns:
                    valid_sections = main_df[main_df["Menu Location"].isin(["Services", "Rental", "Rentals", "Discount & Promotions", "Recommendation"])]
                    valid_cat_names = valid_sections["Title"].unique().tolist()
                    
                    if "Sub-category" in design_df.columns:
                        mask = design_df["Category"].isin(valid_cat_names)
                        relevant = design_df[mask]
                        for _, row in relevant.drop_duplicates(subset=["Sub-category"]).iterrows():
                            sub = row.get("Sub-category")
                            if sub:
                                our_services.append({"name": sub, "category": row.get("Category")})
        except Exception as cache_err:
            print(f"[intelligent_service_check] Cache read error (non-fatal): {cache_err}")

        # Always include core hardcoded services to ensure reliability for Martial Arts, Photography, etc.
        for _, info in self.service_categories.items():
            if not any(s['name'].lower() == info["subcategory"].lower() for s in our_services):
                our_services.append({"name": info["subcategory"], "category": info["category"]})
        
        services_list = "\n".join([f"- {s['name']} ({s['category']})" for s in our_services])
        
        prompt = f"""You are a Concierge Service Matcher.
        
AVAILABLE SERVICES:
{services_list}

USER QUERY: "{query}"

TASK:
1. Is this a request for a service, or providing details (dates/times) specifically for one of our services? 
   Note: Mentioning items like "Scoopy", "Scooter", "Massage", "NMAX", "Bike" counts as a service request.
2. Does it match our list above?

Return ONLY valid JSON:
{{{{
    "is_service_request": true/false,
    "requested_service": "name",
    "we_offer_it": true/false,
    "matched_service": "exact name from AVAILABLE SERVICES above",
    "confidence": float
}}}}
"""
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a precise AI. Return only JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            result = json.loads(response.choices[0].message.content)
            self._service_check_cache[_cache_key] = result
            return result
        except Exception as e:
            print(f"[intelligent_service_check] Error: {e}")
            return {"is_service_request": False, "we_offer_it": False}

    def detect_service_intent(self, query: str) -> Optional[Dict[str, str]]:
        """Detect which service category user is asking about using keywords"""
        query_lower = query.lower()
        scores = {}
        for service_type, info in self.service_categories.items():
            score = 0
            for keyword in info.get("keywords", []):
                if keyword in query_lower:
                    score += len(keyword.split())
            if score > 0:
                scores[service_type] = score
        
        if scores:
            service_type = max(scores.items(), key=lambda x: x[1])[0]
            info = self.service_categories[service_type]
            return {"service_type": service_type, "category": info["category"], "subcategory": info["subcategory"]}
        return None

    def extract_requirements(self, query: str) -> Dict[str, Any]:
        """Extract parameters like location, people, budget, time, date"""
        reqs = {"location": None, "people_count": None, "budget": None, "time_preference": None, "date": None}
        
        locations = ["canggu", "seminyak", "uluwatu", "ubud", "berawa", "umalas", "kerobokan"]
        for loc in locations:
            if loc in query.lower():
                reqs["location"] = loc.title()
                break
        
        match_pax = re.search(r'(\d+)\s*(?:people|person|pax|guests?)', query.lower())
        if match_pax: reqs["people_count"] = int(match_pax.group(1))

        match_budget = re.search(r'(?:max|budget)\s*(?:idr)?\s*(\d+(?:k|000)?)', query.lower())
        if match_budget: reqs["budget"] = int(match_budget.group(1).replace('k', '000'))
        
        if any(kw in query.lower() for kw in ["morning", "am"]): reqs["time_preference"] = "morning"
        elif any(kw in query.lower() for kw in ["afternoon", "noon"]): reqs["time_preference"] = "afternoon"
        elif any(kw in query.lower() for kw in ["evening", "night"]): reqs["time_preference"] = "evening"
        
        return reqs

    async def generate_service_menu(self, category: str, subcategory: Optional[str], requirements: Dict[str, Any], _villa_code: str = "WEB_VILLA_01") -> Optional[Dict[str, Any]]:
        """
        Build a service selection menu.
        Primary: Prices Set tab (price_distribution cache) — authoritative prices + location filtering.
        Fallback: Services Overview + Archive (services_df cache) when Prices Set cache is empty.
        """
        from app.utils.formatters import clean_price_string

        # ── Primary path: Prices Set tab ─────────────────────────────────────
        price_df = cache.get("price_distribution")
        if price_df is not None and not price_df.empty:
            # Resolve location zone from villa code
            zone = ""
            if _villa_code and _villa_code not in ("WEB_VILLA_01", ""):
                try:
                    from app.services.menu_services import get_villa_location_by_code
                    zone = await get_villa_location_by_code(_villa_code) or ""
                except Exception:
                    pass
            if not zone and requirements.get("location"):
                zone = requirements["location"]
            zone_lower = zone.lower().strip()

            cat_col    = next((c for c in price_df.columns if c.strip().lower() == "category"), None)
            subcat_col = next((c for c in price_df.columns if c.strip().lower() == "sub-category"), None)
            item_col   = next((c for c in price_df.columns if c.strip().lower() == "service item"), None)
            target_cat = category.lower().strip()
            target_sub = subcategory.lower().strip() if subcategory else None

            rows = []
            for _, row in price_df.iterrows():
                raw_price = str(row.get("Final Price (Service Item Button)", "") or "").strip()
                numeric = re.sub(r"[^\d]", "", raw_price)
                if not numeric or numeric == "0":
                    continue

                row_cat = str(row.get(cat_col, "") or "").strip().lower() if cat_col else ""
                if row_cat != target_cat and row_cat != target_cat + "s" and row_cat + "s" != target_cat:
                    if target_cat not in row_cat and row_cat not in target_cat:
                        continue

                if target_sub and subcat_col:
                    row_sub = str(row.get(subcat_col, "") or "").strip().lower()
                    if row_sub != target_sub:
                        continue

                # Location filter: skip only when zone is known AND row has a location restriction that doesn't match
                locs_raw = row.get("Locations", "")
                locs_str = ("" if (locs_raw is None or (isinstance(locs_raw, float) and locs_raw != locs_raw))
                            else str(locs_raw).strip())
                if locs_str and locs_str.lower() not in ("", "nan") and zone_lower:
                    if not re.search(r"\b" + re.escape(zone_lower) + r"\b", locs_str.lower()):
                        continue

                service_name = str(row.get(item_col, "") or "").strip() if item_col else ""
                if not service_name:
                    continue

                idx = len(rows)
                rows.append({
                    "id": f"ai_service_{idx}_{service_name.replace(' ', '_')}",
                    "title": service_name,
                    "full_title": service_name,
                    "description": "",
                    "price": clean_price_string(raw_price),
                })

            if rows:
                img_url = self.subcategory_image_urls.get(subcategory) if subcategory else None
                return {
                    "title": (subcategory or category)[:60],
                    "description": f"Found {len(rows)} service(s) matching your request.",
                    "image_url": img_url,
                    "sections": [{"title": "Available Services", "rows": rows[:15]}],
                }

        # ── Fallback: services_df (Services Overview + Archive) ──────────────
        from app.services.google_sheets_service import google_sheets_service
        all_services = await google_sheets_service.get_services_data()
        if not all_services:
            return None

        filtered = []
        target_cat = category.lower().strip()
        for s in all_services:
            db_cat = s["category"].lower().strip()
            if db_cat != target_cat and db_cat != target_cat + "s" and db_cat + "s" != target_cat:
                if target_cat not in db_cat and db_cat not in target_cat:
                    continue
            if subcategory and s["subcategory"].lower() != subcategory.lower():
                continue
            if requirements.get("location"):
                loc = requirements["location"].lower()
                if loc not in s["villa_code"].lower() and s["villa_code"].lower() != "all":
                    continue
            filtered.append(s)

        if not filtered:
            return None

        if requirements.get("budget"):
            budget = requirements["budget"]
            filtered = [s for s in filtered if int(re.sub(r"[^\d]", "", str(s.get("price", "0"))) or 0) <= budget]

        if not filtered:
            return None

        rows = []
        for idx, service in enumerate(filtered[:15]):
            name = service.get("service_name", "Unknown Service")
            rows.append({
                "id": f"ai_service_{idx}_{name.replace(' ', '_')}",
                "title": name,
                "full_title": name,
                "description": service.get("description", "")[:72],
                "price": clean_price_string(service.get("price", "")),
            })

        if not rows:
            return None

        img_url = self.subcategory_image_urls.get(subcategory) if subcategory else None
        return {
            "title": (subcategory or category)[:60],
            "description": f"Found {len(rows)} service(s) matching your request.",
            "image_url": img_url,
            "sections": [{"title": "Available Services", "rows": rows}],
        }

    async def get_service_details_by_id(self, service_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a service by its generated ID"""
        from app.services.google_sheets_service import google_sheets_service
        all_services = await google_sheets_service.get_services_data()
        if not all_services: return None
        
        parts = service_id.split("_")
        if len(parts) >= 3:
            s_name = "_".join(parts[2:]).replace('_', ' ').lower()
            for s in all_services:
                if s['service_name'].lower() == s_name:
                    return s
        return None

ai_menu_generator = AIMenuGenerator()