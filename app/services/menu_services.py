import pandas as pd
from app.services.google_sheets import get_workbook
from app.utils.data_processing import clean_dataframe
import threading
import re
from datetime import datetime

import os
import logging

logger = logging.getLogger(__name__)

# Google Sheet configurations
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1tuGBnQFjDntJQglofA17uHhiyekkVyDoSInErbwfR24")
AI_MATERIAL_SHEET_ID = "1u-bZneHYE2OAURxyuy9RjVO3HEZCn6hWByHisLDg_qI"


def _parse_sheet_hyperlink(url: str):
    """Extract (sheet_id, gid) from a Google Sheets URL, e.g.
    'https://docs.google.com/spreadsheets/d/<ID>/edit?gid=<N>#gid=<N>'.
    Returns (None, None) if the URL doesn't look like a Sheets link."""
    if not url:
        return None, None
    id_match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not id_match:
        return None, None
    gid_match = re.search(r"[#?&]gid=(\d+)", url)
    gid = int(gid_match.group(1)) if gid_match else 0
    return id_match.group(1), gid

_workbook = None
_ai_material_workbook = None

def get_cached_workbook():
    global _workbook
    if _workbook is None:
        from app.services.google_sheets import get_workbook
        _workbook = get_workbook(SHEET_ID)
    return _workbook

def get_ai_material_workbook():
    global _ai_material_workbook
    if _ai_material_workbook is None:
        from app.services.google_sheets import get_workbook
        _ai_material_workbook = get_workbook(AI_MATERIAL_SHEET_ID)
    return _ai_material_workbook

# Cache for storing data
cache = {
    "menu_df": None,
    "services_df": None,
    "design_df": None,
    "last_updated": None,
    "main_menu_design": None,
    "service_providers": None,
    "villas_data": None,
    "price_distribution": None,
    "archive_df": None,
    "platform_design_df": None,
    "price_diff_df": None,
    "price_diff_sp_df": None,
    "language_lesson_df": None,
    "event_calendar_df": None,
    "dnp_df": None,
    # Menu Structure "Hybrid AI"/"AI Automated" endpoint cells carry a hyperlink
    # that is normally discarded (see the Menu Structure hyperlink-extraction
    # block below). Rows whose data should be dynamically fetched from wherever
    # that hyperlink points (currently: Event Calendar) get captured here,
    # keyed by lowercased Sub-category, instead of being silently dropped.
    "ai_endpoint_links": {},
}

# Keys included in /menu/cache-status row-count report
_CACHE_STATUS_KEYS = [
    "price_distribution",
    "villas_data",
    "services_df",
    "design_df",
    "menu_df",
    "main_menu_design",
    "service_providers",
    "dnp_df",
]

# Refresh metadata — written by _do_refresh() in main_menu_routes.py
_refresh_meta: dict = {
    "refresh_in_progress": False,
    "last_refresh_source": None,      # "apps_script" | "manual" | "startup" | "unknown"
    "last_refresh_start": None,       # ISO string (UTC)
    "last_refresh_end": None,         # ISO string (UTC)
    "last_refresh_success": None,     # True | False | None (never refreshed)
    "last_refresh_error": None,       # error message string or None
    "last_successful_refresh": None,  # last_updated value from a successful refresh
    "sheets_row_counts": {},          # key → row count from last successful refresh
}


def clear_cache():
    """Reset all cached data to None."""
    global cache
    for k in cache.keys():
        cache[k] = None
    logger.info("Cache cleared.")

# def validate_design_df() -> pd.DataFrame:
    """Return a validated design dataframe. Load into cache if missing."""
    logger.info("design_df not in cache - loading data now")
    load_data_into_cache()
    df = cache.get("design_df")
    if df is not None:
        # Ensure required columns are present
        required = {"Category", "Category Description", "Category Picture", "Category Button"}
        missing = required - set(df.columns)
        if missing:
            logger.warning(f"design_df missing columns: {missing}")
        # Normalise category strings
        df["Category"] = df["Category"].astype(str).str.strip().str.title()
    return df

# Refresh thread control
should_stop = False
refresh_thread = None

def load_data_into_cache():
    """Load all Google Sheets data into an isolated staging dict, then atomically swap
    into the live cache.  Readers always see a complete (if possibly stale) snapshot -
    never a partial state."""
    print(f"Refreshing data at {datetime.now()}...")
    try:
        workbook = get_cached_workbook()

        # ── Staging dict — all writes go here, live cache is untouched until commit ──
        _new: dict = {k: cache.get(k) for k in cache}  # seed with current values as fallback

        def safe_load(sheet_name, cache_key, use_clean=True):
            try:
                ws = workbook.worksheet(sheet_name)
                data = ws.get_all_values()
                if not data:
                    logger.warning(f"Sheet '{sheet_name}' is empty.")
                    return
                if use_clean:
                    _new[cache_key] = clean_dataframe(data)
                else:
                    _new[cache_key] = pd.DataFrame(data[1:], columns=data[0])
                logger.info(f"✅ Loaded sheet: {sheet_name}")
            except Exception as e:
                logger.error(f"❌ Failed to load sheet '{sheet_name}': {e}")

        # ── Menu Structure — special handling to extract hyperlink URLs ──────────────
        try:
            _new["ai_endpoint_links"] = {}  # reset fresh each cycle — never accumulate stale entries
            _ms_ws = workbook.worksheet("Menu Structure")
            _ms_values = _ms_ws.get_all_values()
            if _ms_values:
                _ms_headers = _ms_values[0]
                _ep_col = _ms_headers.index("Endpoint") if "Endpoint" in _ms_headers else None
                # Sub-categories that must ALWAYS use their sheet hyperlink URL (never AI text)
                _LINK_ONLY_SUBCATS = {
                    "safety and health tips", "safety & health tips",
                    "medical recommendations", "medical reccomendations", "medical reccomendation",
                    "do's and don'ts of bali", "do's and don't of bali", "dos and don'ts of bali",
                    "dos and don'ts", "do's and don'ts",
                    "local cuisine guide", "local cousine guide",
                }
                _sub_col = _ms_headers.index("Sub-category") if "Sub-category" in _ms_headers else None
                # WCR-EVCAL-01 (2026-08-18): for "Bali Handbook" rows the identifying
                # label ("Event Calendar", "Safety & Health Tips", "Local Cuisine
                # Guide", etc.) lives in the CATEGORY column — Sub-category is blank
                # for every one of these rows. Matching on Sub-category alone (the
                # prior behavior) meant _is_force_link was silently always False for
                # any Bali Handbook row whose Endpoint text starts with "Hybrid AI"/
                # "AI Automated" — "Local Cuisine Guide" (Endpoint = "Hybrid AI Result
                # - Local Cuisine Guide") was in _LINK_ONLY_SUBCATS but its hyperlink
                # was never actually being force-extracted. Category is now checked
                # first (falling back to Sub-category so nothing that worked before
                # can regress).
                _cat_col = _ms_headers.index("Category") if "Category" in _ms_headers else None
                if _ep_col is not None:
                    try:
                        _hl_resp = workbook.client.request(
                            "GET",
                            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}",
                            params={
                                "ranges": "Menu Structure",
                                "includeGridData": "true",
                                "fields": "sheets.data.rowData.values(hyperlink)",
                            },
                        )
                        _row_data = (
                            _hl_resp.json()
                            .get("sheets", [{}])[0]
                            .get("data", [{}])[0]
                            .get("rowData", [])
                        )
                        for _ri, _rinfo in enumerate(_row_data):
                            if _ri == 0:
                                continue  # skip header
                            _cells = _rinfo.get("values", [])
                            if _ep_col < len(_cells):
                                _hl = _cells[_ep_col].get("hyperlink")
                                _subcat_val = ""
                                if _sub_col is not None and _sub_col < len(_ms_values[_ri]):
                                    _subcat_val = _ms_values[_ri][_sub_col].lower().strip()
                                _cat_val = ""
                                if _cat_col is not None and _cat_col < len(_ms_values[_ri]):
                                    _cat_val = _ms_values[_ri][_cat_col].lower().strip()
                                _match_val = _cat_val or _subcat_val
                                _is_force_link = _match_val in _LINK_ONLY_SUBCATS
                                _display = (
                                    _ms_values[_ri][_ep_col]
                                    if _ep_col < len(_ms_values[_ri]) else ""
                                ).lower()
                                _is_ai_endpoint = (
                                    _display.startswith("hybrid ai")
                                    or _display.startswith("ai automated")
                                )
                                if _hl and (not _is_ai_endpoint or _is_force_link):
                                    while len(_ms_values[_ri]) <= _ep_col:
                                        _ms_values[_ri].append("")
                                    _ms_values[_ri][_ep_col] = _hl
                                elif _hl and _is_ai_endpoint and _match_val:
                                    # Capture instead of discarding — a "Hybrid AI"/
                                    # "AI Automated" cell's hyperlink still points at a
                                    # real data source (e.g. Event Calendar); the display
                                    # text stays as-is, but downstream loaders can now
                                    # follow this link to fetch dynamically.
                                    _new.setdefault("ai_endpoint_links", {})[_match_val] = _hl
                    except Exception as _hl_err:
                        logger.warning(f"Could not extract Menu Structure hyperlinks: {_hl_err}")
                from app.utils.data_processing import clean_dataframe as _cdf
                _new["menu_df"] = _cdf(_ms_values)
                logger.info("✅ Loaded sheet: Menu Structure (with hyperlinks)")
        except Exception as _ms_err:
            logger.error(f"❌ Failed to load Menu Structure: {_ms_err}")
            safe_load("Menu Structure", "menu_df")  # fallback without hyperlinks

        # ── Standard sheets ───────────────────────────────────────────────────────────
        safe_load("Services Overview", "services_df")
        safe_load("Prices Set", "price_distribution")
        # "Prices Set" uses "Service Items" (plural) — normalise to "Service Item"
        if _new.get("price_distribution") is not None and "Service Items" in _new["price_distribution"].columns:
            _new["price_distribution"] = _new["price_distribution"].rename(columns={"Service Items": "Service Item"})
        safe_load("Services Providers", "service_providers")
        safe_load("Villas", "villas_data")
        safe_load("Services Designs", "design_df")
        safe_load("Menu Design", "main_menu_design")

        # ── Archive: rental items (bike, car) — positional columns, no standard headers ─
        try:
            _arch_ws = workbook.worksheet("Archive")
            _arch_data = _arch_ws.get_all_values()
            if _arch_data and len(_arch_data) > 1:
                _arch_rows = []
                for _row in _arch_data[1:]:
                    # Schema: [0]=Category, [1]=Sub-category, [2]=Service Item,
                    # [3]=Vendor Price, [7]=Final Price (may contain #REF!)
                    if len(_row) >= 3 and _row[0].strip() and _row[2].strip():
                        from app.utils.formatters import clean_price_string
                        _price_raw = _row[7].strip() if len(_row) > 7 else (_row[3].strip() if len(_row) > 3 else "")
                        _final_price = clean_price_string(_price_raw)
                        
                        _arch_rows.append({
                            "Category": _row[0].strip(),
                            "Sub-category": _row[1].strip(),
                            "Sub-category ID": _row[1].strip().lower().replace(" ", "_"),
                            "Service Item": _row[2].strip(),
                            "Service Item Description": "",
                            "Final Price (Service Item Button)": _final_price,
                            "Locations": "",
                            "Service Providers": "",
                            "Image URL": "",
                        })
                if _arch_rows:
                    _arch_df = pd.DataFrame(_arch_rows)
                    _new["archive_df"] = _arch_df
                    # Merge into staging services_df so all menu/booking functions see rentals
                    if _new.get("services_df") is not None and not _new["services_df"].empty:
                        _new["services_df"] = pd.concat(
                            [_new["services_df"], _arch_df], ignore_index=True
                        ).fillna("")
                    else:
                        _new["services_df"] = _arch_df
                    logger.info(f"✅ Loaded Archive: {len(_arch_rows)} rental items merged into services")
        except Exception as _arch_err:
            logger.error(f"❌ Failed to load Archive: {_arch_err}")

        safe_load("AI Data", "ai_data_df")

        # ── DNP sheet: Discounts & Promotions (source of truth for guest-facing promos) ─
        # use_clean=False preserves exact column headers from the sheet header row
        safe_load("D&P", "dnp_df", use_clean=False)

        # ── AI Material spreadsheet (separate Google Sheet) ───────────────────────────
        try:
            ai_wb = get_ai_material_workbook()
            def safe_load_ai(sheet_name, cache_key):
                try:
                    ws = ai_wb.worksheet(sheet_name)
                    data = ws.get_all_values()
                    if not data:
                        logger.warning(f"AI Material sheet '{sheet_name}' is empty.")
                        return
                    _new[cache_key] = pd.DataFrame(data[1:], columns=data[0])
                    logger.info(f"✅ Loaded AI Material sheet: {sheet_name}")
                except Exception as e:
                    logger.error(f"❌ Failed to load AI Material sheet '{sheet_name}': {e}")
            safe_load_ai("Local Language Lesson", "language_lesson_df")

            # ── Event Calendar — dynamically follow the Menu Structure hyperlink ──
            # (2026-08-18, Clay/Adam) rather than always reading the hardcoded
            # AI Material sheet. The Menu Structure "Hybrid AI Result - Event
            # Calendar" cell IS a real hyperlink; if it ever gets repointed to a
            # different sheet/tab, this makes that change take effect automatically
            # with no code change. Falls back to the old hardcoded path if the
            # link is missing, malformed, or the linked sheet is unreachable —
            # never let a data-source failure break the feature.
            _ec_loaded = False
            _ec_link = (_new.get("ai_endpoint_links") or {}).get("event calendar")
            if _ec_link:
                _ec_sheet_id, _ec_gid = _parse_sheet_hyperlink(_ec_link)
                if _ec_sheet_id is not None:
                    try:
                        _ec_wb = get_workbook(_ec_sheet_id)
                        _ec_ws = _ec_wb.get_worksheet_by_id(_ec_gid)
                        _ec_data = _ec_ws.get_all_values()
                        if _ec_data:
                            _new["event_calendar_df"] = pd.DataFrame(_ec_data[1:], columns=_ec_data[0])
                            _ec_loaded = True
                            logger.info(
                                f"✅ Loaded Event Calendar dynamically from Menu "
                                f"Structure hyperlink (sheet={_ec_sheet_id}, gid={_ec_gid})"
                            )
                        else:
                            logger.warning("Event Calendar hyperlink target sheet/tab is empty")
                    except Exception as _ec_err:
                        logger.warning(f"Dynamic Event Calendar fetch failed: {_ec_err}")
                else:
                    logger.warning(f"Event Calendar hyperlink did not look like a Sheets URL: {_ec_link!r}")
            if not _ec_loaded:
                logger.info("Falling back to hardcoded AI Material sheet for Event Calendar")
                safe_load_ai("Event Calendar", "event_calendar_df")
        except Exception as e:
            logger.error(f"❌ Failed to connect to AI Material spreadsheet: {e}")

        # ── Atomic commit: update live cache from staging dict ────────────────────────
        # Each dict[key] = value assignment is atomic under CPython's GIL.
        # By updating only after all sheets are loaded, concurrent readers see
        # either the fully-old snapshot or the fully-new one — never a partial mix.
        for _k, _v in _new.items():
            if _k != "last_updated":
                cache[_k] = _v
        cache["last_updated"] = datetime.now()
        logger.info("✅ Cache refresh committed atomically.")

    except Exception as e:
        logger.error(f"Critical error in load_data_into_cache: {e}")

def schedule_data_refresh():
    """Runs data refresh at regular intervals."""
    global should_stop
    while not should_stop:
        load_data_into_cache()
        for _ in range(900):  # Refresh every 15 minutes (900 seconds) to avoid Rate Limits (429)
            if should_stop:
                break
            threading.Event().wait(1)

def start_cache_refresh():
    """Starts the background thread for refreshing data."""
    global refresh_thread
    refresh_thread = threading.Thread(target=schedule_data_refresh, daemon=True)
    refresh_thread.start()

def stop_cache_refresh():
    """Stops the background thread for refreshing data."""
    global should_stop, refresh_thread
    should_stop = True
    if refresh_thread:
        refresh_thread.join()
        refresh_thread = None

async def get_main_menu():
    if cache["menu_df"] is None:
        raise ValueError("Data not loaded")
    return cache["menu_df"]['Main Menu'].dropna().unique().tolist()


async def get_main_menu_design():
    if cache["main_menu_design"] is None:
        raise ValueError("Data not loaded")
    return cache["main_menu_design"]


async def get_service_providers():
    if cache["service_providers"] is None:
        raise ValueError("Data not loaded")
    return cache["service_providers"]

async def fetch_villas_fresh() -> pd.DataFrame:
    """
    Always fetch the Villas sheet directly from Google Sheets - no cache.

    Column layout (user-confirmed, April 2026):
      A (0)  Number          - V-code (V1, V2 ...)
      B (1)  Name of Villa
      G (6)  Location
      H (7)  Contact (WhatsApp number for notifications)
      Q (16) Bank
      R (17) Account Number

    We match by header name first; if a column header has been renamed we fall
    back to its positional index so the function never silently returns None.
    """
    try:
        wb = get_cached_workbook()          # reuses auth; does NOT cache sheet data
        ws = wb.worksheet("Villas")
        raw = ws.get_all_values()           # always live from Google
        if not raw or len(raw) < 2:
            logger.warning("[Villas] Sheet returned empty data")
            return pd.DataFrame()

        headers = [h.strip() for h in raw[0]]

        # ── Column-index fallback map ─────────────────────────────────────────
        # If a header is missing or renamed, we synthesise it from the column
        # index. Indices are 0-based.
        _IDX_FALLBACKS = {
            "Number":          0,
            "Name of Villa":   1,
            "Location":        6,
            "Contact":         7,   # WhatsApp for villa notifications
            "Bank":            16,
            "Account Number":  17,
        }
        for col_name, col_idx in _IDX_FALLBACKS.items():
            if col_name not in headers and col_idx < len(headers):
                # Insert as alias — patch header row
                headers[col_idx] = col_name

        df = pd.DataFrame(raw[1:], columns=headers)
        df.columns = [c.strip() for c in df.columns]
        df = df.replace(r"^\s+|\s+$", "", regex=True)
        df = df[df["Number"].notna()]
        df = df[df["Number"].str.strip() != ""]
        logger.info(f"[Villas] Fetched {len(df)} rows fresh from sheet")
        return df
    except Exception as e:
        logger.error(f"[Villas] Fresh fetch failed: {e}")
        # Last-resort: return cached snapshot if available so callers don't crash
        if cache.get("villas_data") is not None and not cache["villas_data"].empty:
            logger.warning("[Villas] Returning cached snapshot as emergency fallback")
            return cache["villas_data"]
        return pd.DataFrame()


async def get_villa_data():
    """Returns fresh villa data every call - cache is never used."""
    return await get_villas_df()


async def get_villas_df() -> "pd.DataFrame":
    """Returns the villas DataFrame from cache, or fetches it if missing."""
    df = cache.get("villas_data")
    if df is None or df.empty:
        df = await fetch_villas_fresh()
        cache["villas_data"] = df
    return df

async def get_all_villas():
    """Returns all villas as a list of dictionaries with standard keys. Uses cache."""
    df = await get_villas_df()
    if df.empty:
        return []
    result = []
    for _, row in df.iterrows():
        result.append({
            "code": str(row.get("Number", "")).strip(),
            "name": str(row.get("Name of Villa", "")).strip(),
            "location": str(row.get("Location", "")).strip()
        })
    return result


async def get_price_distribution():
    if cache["price_distribution"] is None:
        raise ValueError("Data not loaded")
    return cache["price_distribution"]


async def get_service_overview():
    if cache["services_df"] is None:
        raise ValueError("Data not loaded")
    return cache["services_df"]



async def get_categories():
    if cache["design_df"] is None:
        raise ValueError("Data not loaded")
    categories = cache["design_df"].drop_duplicates(subset=["Category"])[
        ["Category", "Category Description WA"]
    ]

    result = []

    for _, category_row in categories.iterrows():
        category_name = category_row["Category"]
        category_description = category_row["Category Description WA"]

        subcategories = cache["design_df"][
            cache["design_df"]["Category"] == category_name
        ][["Sub-category", "Sub-category Description WA"]].drop_duplicates()

        section_rows = [
            {
                "id": row["Sub-category"].lower().replace(" ", "_"),
                "service_title": row["Sub-category"],
                "service_description": row["Sub-category Description WA"]
            }
            for _, row in subcategories.iterrows()
        ]
        result.append(
            {
                "title": category_name,  # Category name as header
                "description": category_description,  # Category description as body
                "sections": section_rows  # Sections contain subcategories
            }
        )

    return result



def _get_available_sets_for_zone(zone_lower: str):
    """Return (available_categories_lower, available_subcategories_lower) from Prices Set.

    A category/subcategory is "available" if at least one service item in Prices Set
    has a non-zero price AND its Locations column includes zone_lower (or is empty/unset).
    """
    import re as _re
    price_df = cache["price_distribution"]
    if price_df is None:
        return set(), set()

    cat_col = next((c for c in price_df.columns if c.strip().lower() == 'category'), None)
    subcat_col = next((c for c in price_df.columns if c.strip().lower() == 'sub-category'), None)

    available_categories: set = set()
    available_subcategories: set = set()

    for _, row in price_df.iterrows():
        raw_price = str(row.get("Final Price (Service Item Button)", "") or "").strip()
        if not _re.sub(r'[^\d]', '', raw_price) or _re.sub(r'[^\d]', '', raw_price) == "0":
            continue

        locs_raw = row.get("Locations", "")
        locs_str = "" if (locs_raw is None or (isinstance(locs_raw, float) and locs_raw != locs_raw)) else str(locs_raw).strip()
        if locs_str and locs_str.lower() != 'nan':
            # Use word-boundary match so "Seminyak" is found whether the cell
            # stores "Uluwatu, Seminyak" (comma) or "Uluwatu Seminyak" (chip
            # space-concat) or "Uluwatu\nSeminyak" (newline) — any separator works.
            if not _re.search(r'\b' + _re.escape(zone_lower) + r'\b', locs_str.lower()):
                continue

        if cat_col:
            cat = str(row.get(cat_col, "") or "").strip()
            if cat:
                available_categories.add(cat.lower())
        if subcat_col:
            subcat = str(row.get(subcat_col, "") or "").strip()
            if subcat:
                available_subcategories.add(subcat.lower())

    return available_categories, available_subcategories


async def get_service_catalog_context(villa_code: str = None, location_zone: str = None) -> str:
    """
    Return a formatted service catalog string from the Prices Set cache,
    filtered by the guest's location zone.

    Used by AI to answer "what services do you have?" queries — ensuring
    recommendations come strictly from the Google Sheets source of truth.
    Returns "" if cache is unavailable (non-fatal).
    """
    import re as _re
    try:
        price_df = cache.get("price_distribution")
        if price_df is None or price_df.empty:
            return ""

        # Resolve location zone from villa code when not supplied directly
        zone = location_zone or ""
        if not zone and villa_code and villa_code not in ("WEB_VILLA_01", ""):
            zone = await get_villa_location_by_code(villa_code) or ""
        zone_lower = zone.lower().strip()

        cat_col  = next((c for c in price_df.columns if c.strip().lower() == "category"), None)
        item_col = next((c for c in price_df.columns if c.strip().lower() == "service item"), None)
        price_col = "Final Price (Service Item Button)"
        loc_col   = "Locations"

        catalog: dict = {}  # category -> list of "service — IDR price"
        for _, row in price_df.iterrows():
            raw_price = str(row.get(price_col, "") or "").strip()
            numeric   = _re.sub(r"[^\d]", "", raw_price)
            if not numeric or numeric == "0":
                continue

            # Location filter — empty Locations cell means available everywhere
            if zone_lower:
                locs_raw = row.get(loc_col, "")
                locs_str = "" if (locs_raw is None or (isinstance(locs_raw, float) and locs_raw != locs_raw)) else str(locs_raw).strip()
                if locs_str and locs_str.lower() != "nan":
                    if not _re.search(r"\b" + _re.escape(zone_lower) + r"\b", locs_str.lower()):
                        continue

            category     = str(row.get(cat_col,  "") or "").strip() if cat_col  else ""
            service_name = str(row.get(item_col, "") or "").strip() if item_col else ""
            if not category or not service_name:
                continue

            try:
                formatted_price = f"IDR {int(numeric):,}".replace(",", ".")
            except (ValueError, OverflowError):
                formatted_price = raw_price

            catalog.setdefault(category, []).append(f"{service_name} — {formatted_price}")

        if not catalog:
            return ""

        lines = [
            "AVAILABLE EASYBALI SERVICES (live Google Sheets catalog — "
            "ONLY recommend services from this list, never invent or add others):"
        ]
        for cat, items in catalog.items():
            lines.append(f"\n{cat}:")
            for item in items:
                lines.append(f"  • {item}")
        return "\n".join(lines)

    except Exception as _e:
        logger.warning(f"get_service_catalog_context failed (non-fatal): {_e}")
        return ""


async def get_categories_only(location_zone: str = None, villa_code: str = None):
    if cache["design_df"] is None:
        raise ValueError("Data not loaded")

    resolved_zone = None
    if villa_code:
        resolved_zone = await get_villa_location_by_code(villa_code)
    if not resolved_zone and location_zone:
        resolved_zone = location_zone.strip()
    if not resolved_zone:
        resolved_zone = 'Seminyak'  # default zone for anonymous users without villa context

    available_cats = None
    if resolved_zone and cache["price_distribution"] is not None:
        available_cats, _ = _get_available_sets_for_zone(resolved_zone.lower())

    categories = cache["design_df"].drop_duplicates(subset=["Category"])[
        ["Category", "Category ID", "Category Description WA"]
    ]

    result = []
    for _, category_row in categories.iterrows():
        category_name = category_row["Category"]
        if available_cats is not None and category_name.lower() not in available_cats:
            continue
        result.append(
            {
                "id": category_row["Category ID"],
                "title": category_name,
                "description": category_row["Category Description WA"]
            }
        )

    # Fallback: if zone filter excluded everything, return all categories unfiltered
    if not result:
        for _, category_row in categories.iterrows():
            result.append(
                {
                    "id": category_row["Category ID"],
                    "title": category_row["Category"],
                    "description": category_row["Category Description WA"]
                }
            )

    return result

async def get_category_sections(category_title: str, location_zone: str = None, villa_code: str = None):
    if cache["design_df"] is None:
        raise ValueError("Data not loaded")
    if category_title not in cache["design_df"]["Category"].values:
        raise ValueError(f"Category '{category_title}' not found")

    resolved_zone = None
    if villa_code:
        resolved_zone = await get_villa_location_by_code(villa_code)
    if not resolved_zone and location_zone:
        resolved_zone = location_zone.strip()
    if not resolved_zone:
        resolved_zone = 'Seminyak'  # default zone for anonymous users without villa context

    available_subcats = None
    if resolved_zone and cache["price_distribution"] is not None:
        _, available_subcats = _get_available_sets_for_zone(resolved_zone.lower())

    subcategories = cache["design_df"][
        cache["design_df"]["Category"] == category_title
    ][["Sub-category", "Sub-category Description WA"]].drop_duplicates()

    section_rows = []
    for _, row in subcategories.iterrows():
        sub = row["Sub-category"]
        if available_subcats is not None and sub.lower() not in available_subcats:
            continue
        section_rows.append(
            {
                "id": sub.lower().replace(" ", "_"),
                "title": sub,
                "description": row["Sub-category Description WA"]
            }
        )
    return section_rows


async def get_sub_menu(menu_location: str):
    if cache.get("main_menu_design") is None:
        raise ValueError("Main menu design data not loaded")
    
    df = cache["main_menu_design"]
    filtered_df = df[df["Menu Location"] == menu_location]
    if filtered_df.empty:
        raise ValueError(f"Menu location '{menu_location}' not found")
    
    # Get the main title and description from the row where Title matches Menu Location
    main_row = df[df["Title"] == menu_location]
    if main_row.empty:
        main_title = menu_location
        main_description = ""
    else:
        main_title = main_row.iloc[0]["Title"]
        main_description = main_row.iloc[0].get("Description WA", "")
    
    items = [
        {
            "category": row["Title"],
            "description": row["Description"],
            "picture": row["Picture"],
            "button": row["Button"],
            "endpoint": ""
        }
        for _, row in filtered_df.drop_duplicates(subset=["Title"]).iterrows()
    ]

    # Inject endpoint URLs from menu_df — use first-word fuzzy match to bridge
    # name discrepancies between sheets (e.g. "Medical Suggestions" vs "Medical Recommendation")
    _mdf = cache.get("menu_df")
    if _mdf is not None and not _mdf.empty and "Main Menu" in _mdf.columns:
        _menu_rows = _mdf[_mdf["Main Menu"].str.strip() == menu_location.strip()]
        for item in items:
            _first = item["category"].lower().split()[0] if item["category"] else ""
            for _, _mrow in _menu_rows.iterrows():
                _cat = str(_mrow.get("Category", "")).strip().lower()
                _ep = str(_mrow.get("Endpoint", "")).strip()
                if _ep.startswith("http") and _cat.split()[0] == _first:
                    item["endpoint"] = _ep
                    break

    return {
        "main_title": main_title,
        "main_description": main_description,
        "items": items
    }


async def get_restaurants_menu(menu_location: str):
    if cache.get("main_menu_design") is None:
        raise ValueError("Main menu design data not loaded")
    
    df = cache["main_menu_design"]
    filtered_df = df[df["Menu Location"] == menu_location]
    if filtered_df.empty:
        raise ValueError(f"Menu location '{menu_location}' not found")
    return [
        {
            "category": row["Title"],
            "description": row["Description"],
            "picture": row["Picture"],
            "button": row["Button"],
            "Description":row["Description WA"]
        }
        for _, row in filtered_df.drop_duplicates(subset=["Title"]).iterrows()
    ]



async def get_order_service_sub_menu(main_menu: str, location_zone: str = None, villa_code: str = None):
    if main_menu == "Order Services":
        if cache["design_df"] is None:
            raise ValueError("Services data not loaded")

        resolved_zone = None
        if villa_code:
            resolved_zone = await get_villa_location_by_code(villa_code)
        if not resolved_zone and location_zone:
            resolved_zone = location_zone.strip()
        if not resolved_zone:
            resolved_zone = 'Seminyak'  # default zone for anonymous users without villa context

        available_cats = None
        if resolved_zone and cache["price_distribution"] is not None:
            available_cats, _ = _get_available_sets_for_zone(resolved_zone.lower())

        result = []
        for _, row in cache["design_df"].drop_duplicates(subset=["Category"]).iterrows():
            cat_name = row["Category"]
            if available_cats is not None and cat_name.lower() not in available_cats:
                continue
            result.append({
                "category": cat_name,
                "description": row["Category Description"],
                "picture": row["Category Picture"],
                "button": row["Category Button"]
            })
        return result
    if cache["menu_df"] is None:
        raise ValueError("Menu data not loaded")
    
    filtered_df = cache["menu_df"][cache["menu_df"]['Main Menu'] == main_menu]
    if filtered_df.empty:
        raise ValueError("Main menu not found")
    return [
        {
            "category": row["Category"],
            "description": row["Category Description"],
            "picture": row["Category Picture"],
            "button": row["Category Button"]
        }
        for _, row in filtered_df.drop_duplicates(subset=["Category"]).iterrows()
    ]

async def get_sub_sub_menu(main_menu: str, sub_menu: str):
    if cache["menu_df"] is None:
        raise ValueError("Menu data not loaded")
    filtered_df = cache["menu_df"][
        (cache["menu_df"]['Main Menu'] == main_menu) & (cache["menu_df"]['Category'] == sub_menu)
    ]
    if filtered_df.empty:
        raise ValueError("Sub-menu not found")
    return filtered_df['Sub-category'].dropna().tolist()


async def get_sub_category(category: str, location_zone: str = None, villa_code: str = None):
    if cache["design_df"] is None:
        raise ValueError("Services data not loaded")
    filtered_df = cache["design_df"][cache["design_df"]['Category'] == category]
    if filtered_df.empty:
        alt = (category + 's') if not category.endswith('s') else category[:-1]
        filtered_df = cache["design_df"][cache["design_df"]['Category'] == alt]
        if not filtered_df.empty:
            category = alt
    if filtered_df.empty:
        raise ValueError("Category not found")

    resolved_zone = None
    if villa_code:
        resolved_zone = await get_villa_location_by_code(villa_code)
    if not resolved_zone and location_zone:
        resolved_zone = location_zone.strip()
    if not resolved_zone:
        resolved_zone = 'Seminyak'  # default zone for anonymous users without villa context

    available_subcats = None
    if resolved_zone and cache["price_distribution"] is not None:
        _, available_subcats = _get_available_sets_for_zone(resolved_zone.lower())

    # Build endpoint URL lookup from menu_df (hyperlinks extracted at cache load time)
    _endpoint_lookup = {}
    _mdf = cache.get("menu_df")
    if _mdf is not None and not _mdf.empty and "Sub-category" in _mdf.columns and "Endpoint" in _mdf.columns:
        for _, _mrow in _mdf.iterrows():
            _sub_key = str(_mrow.get("Sub-category", "")).strip().lower()
            _ep_val = str(_mrow.get("Endpoint", "")).strip()
            if _sub_key and _ep_val.startswith("http"):
                _endpoint_lookup[_sub_key] = _ep_val

    design_subcats = set()
    result = []
    for _, row in filtered_df.drop_duplicates(subset=["Sub-category"]).iterrows():
        sub = str(row["Sub-category"]).strip()
        if not sub:
            continue
        if available_subcats is not None and sub.lower() not in available_subcats:
            continue
        design_subcats.add(sub)
        item = {
            "subcategory": sub,
            "description": row["Sub-category Description"],
            "picture": row["Sub-category Picture"],
            "button": row["Sub-category Button"],
        }
        _link = _endpoint_lookup.get(sub.lower())
        if _link:
            item["link"] = _link
        result.append(item)

    # Add subcategories from Archive (e.g. Bike/Car rentals)
    if cache["services_df"] is not None:
        svc_for_cat = cache["services_df"][
            cache["services_df"]["Category"].str.strip() == category
        ]
        for sub in svc_for_cat["Sub-category"].dropna().unique():
            sub_clean = str(sub).strip()
            if not sub_clean or sub_clean in design_subcats:
                continue
            if available_subcats is not None and sub_clean.lower() not in available_subcats:
                continue
            item = {
                "subcategory": sub_clean,
                "description": f"{sub_clean} rental options",
                "picture": "",
                "button": "See Items",
            }
            _link = _endpoint_lookup.get(sub_clean.lower())
            if _link:
                item["link"] = _link
            design_subcats.add(sub_clean)
            result.append(item)

    # Add subcategories from Prices Set that have no design or archive entry.
    # Ensures a service added only to Prices Set is immediately visible without
    # requiring a matching row in Services Designs first.
    price_df = cache.get("price_distribution")
    if price_df is not None:
        _pc_cat = next((c for c in price_df.columns if c.strip().lower() == "category"), None)
        _pc_sub = next((c for c in price_df.columns if c.strip().lower() == "sub-category"), None)
        if _pc_cat and _pc_sub:
            mask = price_df[_pc_cat].apply(lambda v: str(v).strip().lower() == category.lower())
            for sub in price_df.loc[mask, _pc_sub].dropna().unique():
                sub_clean = str(sub).strip()
                if not sub_clean or sub_clean in design_subcats:
                    continue
                if available_subcats is not None and sub_clean.lower() not in available_subcats:
                    continue
                design_subcats.add(sub_clean)
                _link = _endpoint_lookup.get(sub_clean.lower())
                item = {
                    "subcategory": sub_clean,
                    "description": f"Book your {sub_clean} session.",
                    "picture": "",
                    "button": "See Items",
                }
                if _link:
                    item["link"] = _link
                result.append(item)

    return result


async def get_service_items(subcategory: str, villa_code: str = None, location_zone: str = None):
    if cache["services_df"] is None:
        raise ValueError("Services data not loaded")

    # Resolve location zone: villa_code takes priority, then explicit location_zone
    resolved_zone = None
    if villa_code:
        resolved_zone = await get_villa_location_by_code(villa_code)
    if not resolved_zone and location_zone:
        resolved_zone = location_zone.strip()
    if not resolved_zone:
        resolved_zone = 'Seminyak'  # default zone for anonymous users without villa context

    from app.utils.formatters import clean_price_string
    import re as _re

    def _locs_match(row, zone_lower):
        locs_raw = row.get("Locations", "")
        locs_str = "" if (locs_raw is None or (isinstance(locs_raw, float) and locs_raw != locs_raw)) else str(locs_raw).strip()
        if not locs_str or locs_str.lower() == 'nan':
            return True  # no location restriction — available everywhere
        # Word-boundary regex handles any separator Google Sheets may use:
        # comma ("Uluwatu, Seminyak"), space chips ("Uluwatu Seminyak"), newline, etc.
        return bool(_re.search(r'\b' + _re.escape(zone_lower) + r'\b', locs_str.lower()))

    # When location is known, use Prices Set (price_distribution) as the primary source.
    # Prices Set is authoritative for which services exist per location (column G) and
    # what price to display (column F). Services Overview is used only to enrich with
    # description, image URL, and service provider code.
    if resolved_zone and cache["price_distribution"] is not None:
        price_df = cache["price_distribution"]

        # Column may be "Sub-category" or "Sub-Category" — find case-insensitively
        subcat_col = next((c for c in price_df.columns if c.strip().lower() == 'sub-category'), None)
        if subcat_col:
            subcat_lower = subcategory.strip().lower()
            pd_filtered = price_df[price_df[subcat_col].str.strip().str.lower() == subcat_lower]

            if not pd_filtered.empty:
                # Filter by location (empty Locations = available everywhere)
                zone_lower = resolved_zone.lower()
                location_filtered = pd_filtered[pd_filtered.apply(lambda r: _locs_match(r, zone_lower), axis=1)]

                if location_filtered.empty:
                    return []

                # Build lookup from Services Overview for description / image / SP code
                svc_lookup = {}
                svc_df = cache["services_df"]
                if svc_df is not None:
                    for _, srow in svc_df.iterrows():
                        name = str(srow.get("Service Item", "") or "").strip()
                        if name:
                            svc_lookup[name.lower()] = srow.to_dict()

                result = []
                seen = set()
                for _, row in location_filtered.iterrows():
                    service_name = str(row.get("Service Item", "") or "").strip()
                    if not service_name or service_name.lower() in seen:
                        continue
                    seen.add(service_name.lower())

                    raw_price = str(row.get("Final Price (Service Item Button)", "") or "").strip()
                    price_str = _re.sub(r'[^\d]', '', raw_price) or "0"
                    if price_str == "0":
                        continue

                    details = svc_lookup.get(service_name.lower(), {})
                    result.append({
                        "service_item": service_name,
                        "description": str(details.get("Service Item Description", "") or "").strip(),
                        "picture": str(details.get("Image URL", "") or "").strip(),
                        "button": clean_price_string(price_str),
                        "service_provider_code": details.get("Service Provider Number", ""),
                    })

                # All service items present in Prices Set for this subcategory (any zone).
                # Used below to prevent Services Overview from overriding Prices Set
                # location restrictions: if an item is in Prices Set for a different zone,
                # it must NOT appear for the current zone via the SO supplement path.
                prices_set_items = set()
                for _, _ps_row in pd_filtered.iterrows():
                    _ps_name = str(_ps_row.get("Service Item", "") or "").strip()
                    if _ps_name:
                        prices_set_items.add(_ps_name.lower())

                # Supplement with Services Overview items absent from Prices Set entirely
                # (e.g. items not yet added to Prices Set but available in Services Overview)
                svc_df_so = cache["services_df"]
                if svc_df_so is not None and "Sub-category" in svc_df_so.columns:
                    so_rows = svc_df_so[svc_df_so["Sub-category"].str.strip().str.lower() == subcat_lower]
                    for _, srow in so_rows.drop_duplicates(subset=["Service Item"]).iterrows():
                        svc_name = str(srow.get("Service Item", "") or "").strip()
                        if not svc_name or svc_name.lower() in seen:
                            continue
                        if svc_name.lower() in prices_set_items:
                            continue  # Prices Set is authoritative for this item's location
                        if not _locs_match(srow, zone_lower):
                            continue
                        raw_so = str(srow.get("Final Price (Service Item Button)", "") or "").strip()
                        price_so = _re.sub(r'[^\d]', '', raw_so) or "0"
                        if price_so == "0":
                            continue
                        seen.add(svc_name.lower())
                        result.append({
                            "service_item": svc_name,
                            "description": str(srow.get("Service Item Description", "") or "").strip(),
                            "picture": str(srow.get("Image URL", "") or "").strip(),
                            "button": clean_price_string(price_so),
                            "service_provider_code": srow.get("Service Provider Number", ""),
                        })

                return result

    # Fallback: no location zone, or Prices Set lacks Sub-category column,
    # or this subcategory has no rows in Prices Set — use Services Overview.
    filtered_df = cache["services_df"][cache["services_df"]['Sub-category'] == subcategory]
    if filtered_df.empty:
        filtered_df = cache["services_df"][
            cache["services_df"]['Sub-category'].str.lower() == subcategory.lower()
        ]
    if filtered_df.empty:
        return []

    result = []
    for _, row in filtered_df.drop_duplicates(subset=["Service Item"]).iterrows():
        if resolved_zone and not _locs_match(row, resolved_zone.lower()):
            continue

        raw = str(row.get("Final Price (Service Item Button)", "") or "").strip()
        price_str = _re.sub(r'[^\d]', '', raw) or "0"
        if price_str == "0":
            continue

        result.append({
            "service_item": row["Service Item"],
            "description": row["Service Item Description"],
            "picture": row["Image URL"],
            "button": clean_price_string(price_str),
            "service_provider_code": row.get("Service Provider Number"),
        })
    return result


async def get_available_zones() -> list:
    """
    Returns all location zone names available in the system.

    Union of two sources so a zone appears in the dropdown the moment it is
    used in EITHER sheet — operators never need a code deploy to add a zone:

      Source 1: Villas tab  — distinct Location values assigned to real villa rows
      Source 2: Prices Set  — all zone names found in the comma-separated
                              'Locations' column (services available per zone)

    Result is sorted alphabetically.
    """
    import re as _re
    zones: set = set()

    # ── Source 1: Villas tab ──────────────────────────────────────────────────
    try:
        villa_df = await fetch_villas_fresh()
        if not villa_df.empty and "Location" in villa_df.columns:
            for loc in villa_df["Location"].dropna():
                loc_str = str(loc).strip()
                if loc_str and loc_str.lower() not in ("", "nan"):
                    zones.add(loc_str)
    except Exception:
        pass

    # ── Source 2: Prices Set Locations column ─────────────────────────────────
    price_df = cache.get("price_distribution")
    if price_df is not None and "Locations" in price_df.columns:
        for locs_raw in price_df["Locations"].dropna():
            locs_str = str(locs_raw).strip()
            if not locs_str or locs_str.lower() == "nan":
                continue
            for part in _re.split(r"[,\n]+", locs_str):
                zone = part.strip()
                if zone and zone.lower() not in ("", "nan"):
                    zones.add(zone)

    return sorted(zones)


async def get_service_base_price(service_name: str) -> str:
    if cache["services_df"] is None:
        raise ValueError("Services data not loaded")
    
    df = cache["services_df"]
    filtered_df = df[df['Service Item'] == service_name]
    
    if filtered_df.empty:
        # Fallback: Normalize all non-alphanumeric characters for maximum matching flexibility
        import re
        norm_input = re.sub(r'[^a-z0-9]', '', str(service_name).lower())
        for idx, row in df.iterrows():
            item = str(row['Service Item'])
            norm_item = re.sub(r'[^a-z0-9]', '', item.lower())
            if norm_input == norm_item or norm_input in norm_item or norm_item in norm_input:
                filtered_df = df.iloc[[idx]]
                break

    if filtered_df.empty:
        print(f"Warning: Service '{service_name}' not found in services data")
        return "0"
        
    price = filtered_df.iloc[0]["Final Price (Service Item Button)"]
    if pd.isna(price):
        return "0"
    # Strip non-breaking spaces, commas, and non-numeric garbage from Google Sheets
    import re as _re
    cleaned = _re.sub(r'[^\d]', '', str(price))
    return cleaned if cleaned else "0"

async def get_location_specific_price(service_name: str, villa_code: str) -> str:
    """Return the location-adjusted total customer price for a service.

    Formula:
        location_total = generic_total + (zone_villa_comm - generic_villa_comm)

    Where:
        generic_total      = Final Price (Service Item Button) from Services Overview
        generic_villa_comm = Villa Comm column in Mark-up sheet
        zone_villa_comm    = location-zone column in Mark-up sheet (e.g. "Seminyak")

    Falls back to the generic Services Overview price when:
        - villa_code is unknown / has no Location in Villas sheet
        - the Mark-up row cannot be found for this service
        - the zone column is absent or blank
    """
    import re as _re

    generic_price_str = await get_service_base_price(service_name)

    if not villa_code:
        return generic_price_str

    location_zone = await get_villa_location_by_code(villa_code)
    if not location_zone:
        return generic_price_str

    try:
        if cache["price_distribution"] is None:
            return generic_price_str

        price_df = cache["price_distribution"]

        # 1. Resolve matching rows for this service
        service_norm = _re.sub(r'[^a-zA-Z0-9]', '', service_name.lower())
        
        # We need to find rows where service matches
        def service_matches(row_val):
            val_norm = _re.sub(r'[^a-zA-Z0-9]', '', str(row_val).lower())
            return service_norm == val_norm or service_norm in val_norm
            
        matching_rows = price_df[price_df["Service Item"].apply(service_matches)]
        
        if matching_rows.empty:
            return generic_price_str

        # 2. Look for a row specifically matching the location_zone
        # The 'Locations' column contains comma-separated zones
        loc_norm = location_zone.strip().lower()
        
        def location_matches(row):
            locs = str(row.get("Locations", "")).lower()
            return loc_norm in [l.strip() for l in locs.split(',')]
            
        specific_match = matching_rows[matching_rows.apply(location_matches, axis=1)]
        
        if not specific_match.empty:
            price_row = specific_match.iloc[0]
            price_val = price_row.get("Final Price (Service Item Button)")
            if pd.notna(price_val):
                cleaned = _re.sub(r'[^\d]', '', str(price_val))
                if cleaned:
                    logger.info(f"✅ Resolved location price for '{service_name}' in '{location_zone}': {cleaned}")
                    return cleaned

        return generic_price_str

    except Exception as e:
        logger.warning(f"get_location_specific_price failed for '{service_name}'/{villa_code}: {e}")
        return generic_price_str


async def get_service_items_for_whatsapp(subcategory_title: str, villa_code: str = None):
    if cache["services_df"] is None:
        raise ValueError("Services data not loaded")

    from app.utils.formatters import clean_price_string
    import re as _re

    resolved_zone = None
    if villa_code:
        resolved_zone = await get_villa_location_by_code(villa_code)
    if not resolved_zone:
        resolved_zone = 'Seminyak'  # default zone for anonymous users without villa context

    def _locs_match_wa(row, zone_lower):
        locs_raw = row.get("Locations", "")
        locs_str = "" if (locs_raw is None or (isinstance(locs_raw, float) and locs_raw != locs_raw)) else str(locs_raw).strip()
        if not locs_str or locs_str.lower() == 'nan':
            return True
        return bool(_re.search(r'\b' + _re.escape(zone_lower) + r'\b', locs_str.lower()))

    # When villa/zone is known, use Prices Set as the authoritative source
    if resolved_zone and cache["price_distribution"] is not None:
        price_df = cache["price_distribution"]

        subcat_col = next((c for c in price_df.columns if c.strip().lower() == 'sub-category'), None)
        if subcat_col:
            def _norm_slash(s: str) -> str:
                return _re.sub(r'\s*/\s*', '/', s.strip().lower())
            subcat_lower = _norm_slash(subcategory_title)
            pd_filtered = price_df[price_df[subcat_col].apply(lambda x: _norm_slash(str(x)) == subcat_lower)]

            if not pd_filtered.empty:
                zone_lower = resolved_zone.lower()
                location_filtered = pd_filtered[pd_filtered.apply(lambda r: _locs_match_wa(r, zone_lower), axis=1)]

                if location_filtered.empty:
                    location_filtered = pd_filtered  # fallback: zone not in Locations, show all items for subcategory

                # Build description lookup from Services Overview
                svc_lookup = {}
                svc_df = cache["services_df"]
                if svc_df is not None:
                    for _, srow in svc_df.iterrows():
                        name = str(srow.get("Service Item", "") or "").strip()
                        if name:
                            svc_lookup[name.lower()] = srow.to_dict()

                result = []
                seen = set()
                for _, row in location_filtered.iterrows():
                    service_name = str(row.get("Service Item", "") or "").strip()
                    if not service_name or service_name.lower() in seen:
                        continue
                    seen.add(service_name.lower())

                    raw_price = str(row.get("Final Price (Service Item Button)", "") or "").strip()
                    clean_price = clean_price_string(raw_price)
                    if not clean_price or clean_price == "0":
                        continue

                    details = svc_lookup.get(service_name.lower(), {})
                    result.append({
                        "title": service_name,
                        "description": str(details.get("Service Item Description", "") or "").strip(),
                        "button": clean_price,
                    })
                return result

    # Fallback: no villa/zone or Prices Set lacks Sub-category — use Services Overview
    filtered_df = cache["services_df"][cache["services_df"]['Sub-category'] == subcategory_title]
    if filtered_df.empty:
        raise ValueError("Category not found")

    result = []
    for _, row in filtered_df.drop_duplicates(subset=["Service Item"]).iterrows():
        if resolved_zone and not _locs_match_wa(row, resolved_zone.lower()):
            continue
        raw_price = str(row.get("Final Price (Service Item Button)", "") or "").strip()
        clean_price = clean_price_string(raw_price)
        result.append({
            "title": row["Service Item"],
            "description": row["Service Item Description"],
            "button": clean_price,
        })
    return result




async def get_service_provider_by_whatsapp(whatsapp_number: str):
    try:
        import re as _re
        providers_df = await get_service_providers()

        # Normalize to digits only, strip leading zeros, ensure 62 prefix
        def _normalize(num: str) -> str:
            digits = _re.sub(r'[^\d]', '', str(num))
            if digits.startswith('0'):
                digits = '62' + digits[1:]
            elif digits.startswith('8'):
                digits = '62' + digits
            return digits

        incoming_norm = _normalize(whatsapp_number)
        providers_df = providers_df.copy()
        providers_df['_wa_norm'] = providers_df["WhatsApp"].apply(_normalize)
        matching_provider = providers_df[providers_df['_wa_norm'] == incoming_norm]

        if matching_provider.empty:
            return None
        return matching_provider.iloc[0]["Number"]

    except Exception as e:
        print(f"Error retrieving service provider: {e}")
        return None
    


async def get_villa_code_by_name(villa_name: str):
    if cache["villas_data"] is None or cache["villas_data"].empty:
        logger.warning("Villa data not loaded into cache.")
        return None
    
    try:
        villas_df = cache["villas_data"]
        search_input = villa_name.strip().lower()
        
        # 1. Search by Number (ID) - Check if input contains the code (robust for 'villa V2')
        for _, row in villas_df.iterrows():
            code = str(row.get("Number", "")).strip().lower()
            if code and (code == search_input or f" {code}" in f" {search_input}"):
                return str(row["Number"]).strip()
        
        # 2. Search by Name - Exact match
        matching_villa = villas_df[
            villas_df["Name of Villa"].astype(str).str.strip().str.lower() == search_input
        ]
        if not matching_villa.empty:
            return str(matching_villa.iloc[0]["Number"]).strip()

        # 3. Search by Name - Check if input contains the name (robust for 'Villa Hassan Umalas')
        for _, row in villas_df.iterrows():
            name = str(row.get("Name of Villa", "")).strip().lower()
            if name and name in search_input:
                return str(row["Number"]).strip()

        # 4. Search by Name - Check if name contains input (Partial match fallback)
        matching_villa = villas_df[
            villas_df["Name of Villa"].astype(str).str.contains(villa_name, case=False, na=False)
        ]
        if not matching_villa.empty:
            return str(matching_villa.iloc[0]["Number"]).strip()
            
        return None
    
    except Exception as e:
        logger.error(f"Error in get_villa_code_by_name: {e}")
        return None
        
    except Exception as e:
        logger.error(f"Error retrieving villa code for '{villa_name}': {e}")
        return None

async def get_villa_location_by_code(villa_code: str):
    """Uses cached villas data."""
    if not villa_code:
        return None
    try:
        df = await get_villas_df()
        if df.empty:
            return None
        match = df[df["Number"].str.strip().str.upper() == villa_code.strip().upper()]
        if match.empty:
            return None
        return match.iloc[0].get("Location") or None
    except Exception as e:
        logger.error(f"[Villas] get_villa_location_by_code error for '{villa_code}': {e}")
        return None


async def get_villa_info_by_code(villa_code: str):
    """
    Retrieves full villa metadata by V-code - uses cached data.

    Column mapping (April 2026):
      A=Number, B=Name of Villa, G=Location, H=Contact (WhatsApp),
      Q=Bank, R=Account Number
    """
    if not villa_code:
        return None
    try:
        df = await get_villas_df()
        if df.empty:
            return None
        match = df[df["Number"].str.strip().str.upper() == villa_code.strip().upper()]
        if match.empty:
            return None
        row = match.iloc[0]

        # Col H — WhatsApp notification number. Try several possible header names.
        manager_number = (
            row.get("Contact")            # synthetic header from index fallback (col H)
            or row.get("Contact of VM")
            or row.get("Contact VM")
            or row.get("Contact of MT")
            or row.get("Contact MT")
            or row.get("Manager Number")
            or row.get("VM Number")
            or ""
        )
        # Col Q — bank name; Col R — account number
        bank = row.get("Bank") or ""
        account_number = row.get("Account Number") or ""

        return {
            "name":             row.get("Name of Villa"),
            "location":         row.get("Location"),
            "address":          row.get("Address"),
            "directions":       row.get("Directions"),
            "manager_name":     row.get("Manager"),
            "manager_number":   manager_number,
            "bank":             bank,
            "account_number":   account_number,
            "wifi_name":        row.get("WiFi Name"),
            "wifi_password":    row.get("WiFi Password"),
            "house_rules":      row.get("Rules"),
            "map_link":         row.get("Map Link"),
        }
    except Exception as e:
        logger.error(f"[Villas] get_villa_info_by_code error for '{villa_code}': {e}")
        return None


# ── Sheet-driven menu navigation (Menu Structure tab) ─────────────────────────

async def get_sheet_menu_categories(main_menu: str) -> list:
    """Return unique categories for a main menu from the Menu Structure sheet."""
    if cache["menu_df"] is None or cache["menu_df"].empty:
        return []
    df = cache["menu_df"]
    filtered = df[df["Main Menu"].str.strip() == main_menu.strip()]
    seen = set()
    result = []
    for _, row in filtered.iterrows():
        cat = str(row.get("Category", "")).strip()
        if cat and cat not in seen:
            seen.add(cat)
            result.append({
                "category": cat,
                "endpoint": str(row.get("Endpoint", "")).strip(),
            })
    return result


async def get_sheet_menu_subcategories(main_menu: str, category: str) -> list:
    """Return subcategories for a main menu + category from the Menu Structure sheet."""
    if cache["menu_df"] is None or cache["menu_df"].empty:
        return []
    df = cache["menu_df"]
    filtered = df[
        (df["Main Menu"].str.strip() == main_menu.strip()) &
        (df["Category"].str.strip() == category.strip())
    ]
    seen = set()
    result = []
    for _, row in filtered.iterrows():
        sub = str(row.get("Sub-category", "")).strip()
        if sub and sub not in seen:
            seen.add(sub)
            result.append({
                "subcategory": sub,
                "endpoint": str(row.get("Endpoint", "")).strip(),
            })
    return result


async def get_sheet_menu_sub_subcategories(main_menu: str, category: str, subcategory: str) -> list:
    """Return sub-subcategories for a 3-level navigation path in the Menu Structure sheet."""
    if cache["menu_df"] is None or cache["menu_df"].empty:
        return []
    df = cache["menu_df"]
    # Try known column name variations
    subsub_col = None
    for col_name in ("Sub-sub-category", "Sub Sub-category", "Sub Sub Category", "Sub-Sub-category", "Sub-sub category"):
        if col_name in df.columns:
            subsub_col = col_name
            break
    if subsub_col is None:
        return []
    filtered = df[
        (df["Main Menu"].str.strip() == main_menu.strip()) &
        (df["Category"].str.strip() == category.strip()) &
        (df["Sub-category"].str.strip() == subcategory.strip())
    ]
    seen = set()
    result = []
    for _, row in filtered.iterrows():
        subsub = str(row.get(subsub_col, "")).strip()
        if subsub and subsub.lower() not in ("nan", "") and subsub not in seen:
            seen.add(subsub)
            result.append({
                "sub_subcategory": subsub,
                "endpoint": str(row.get("Endpoint", "")).strip(),
            })
    return result


async def get_sheet_menu_endpoint(main_menu: str, category: str, subcategory: str = None, sub_subcategory: str = None) -> str:
    """Return the endpoint for a navigation path in the Menu Structure sheet."""
    if cache["menu_df"] is None or cache["menu_df"].empty:
        return ""
    df = cache["menu_df"]
    filtered = df[
        (df["Main Menu"].str.strip() == main_menu.strip()) &
        (df["Category"].str.strip() == category.strip())
    ]
    if subcategory:
        sub_filtered = filtered[filtered["Sub-category"].str.strip() == subcategory.strip()]
        if not sub_filtered.empty:
            filtered = sub_filtered
    if sub_subcategory:
        subsub_col = next(
            (c for c in df.columns if c.lower().replace(" ", "-") in
             ("sub-sub-category", "sub sub-category", "sub-sub category")),
            None
        )
        if subsub_col:
            subsub_filtered = filtered[filtered[subsub_col].str.strip() == sub_subcategory.strip()]
            if not subsub_filtered.empty:
                filtered = subsub_filtered
    if filtered.empty:
        return ""
    return str(filtered.iloc[0].get("Endpoint", "")).strip()


# ── AI Material helpers ────────────────────────────────────────────────────────

def get_language_lesson_words() -> list:
    """Return all language lesson rows from cache as a list of dicts."""
    df = cache.get("language_lesson_df")
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


def get_event_calendar_context() -> str:
    """Return event calendar data as a plain-text block for AI context injection."""
    df = cache.get("event_calendar_df")
    if df is None or df.empty:
        return "No upcoming event data is currently available."
    lines = ["Here are the upcoming events in Bali:"]
    for _, row in df.iterrows():
        name = str(row.get("Event Name", "")).strip()
        if not name:
            continue
        date = str(row.get("Date", "")).strip()
        time = str(row.get("Time", "")).strip()
        location = str(row.get("Location", "")).strip()
        description = str(row.get("Description", "")).strip()
        notes = str(row.get("Additional Notes", "")).strip()
        url = str(row.get("For more details (URL)", row.get("For more details", ""))).strip()
        line = f"- {name}"
        if date:
            line += f" | {date}"
        if time:
            line += f" at {time}"
        if location:
            line += f" | {location}"
        if description:
            line += f"\n  {description}"
        if notes:
            line += f"\n  Note: {notes}"
        if url and url.startswith("http"):
            line += f"\n  More info: {url}"
        lines.append(line)
    return "\n".join(lines)


# ── Discounts & Promotions (DNP sheet helpers) ────────────────────────────────
# Sheet tab: "D&P"
# Exact column names: ID, Category, Sub-category, Endpoints (Message), Title,
# Description, Promo Type, Referral Code, Redemption Method, Commission Type,
# Commission Value, Partner Contact Name, Partner Contact WhatsApp,
# Start Date, End Date, Image URL, Priority, Active

# ─── Generic link → displayable image resolution ────────────────────────────
# Sheet editors may paste ANY link into "Image URL" (a webpage, a stock-photo
# listing, etc.), not only a direct image file. This resolves such links to a
# real, hotlinkable photo via the page's Open Graph / Twitter Card image meta
# tag — the same mechanism Slack/WhatsApp/Twitter use to build link-preview
# cards, so virtually every modern webpage supports it. Cached in-memory per
# URL so a page is fetched at most once per process lifetime. Never raises —
# any failure (timeout, no meta tag, blocked, malformed HTML) returns the
# original URL unchanged so the frontend's own onError fallback still applies.
_DIRECT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".svg")
_resolved_image_url_cache: dict = {}
_BLOCKED_IMAGE_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254")


async def resolve_display_image_url(url: str) -> str:
    """Resolve a sheet 'Image URL' value to something guaranteed displayable.

    Returns the URL unchanged if it already looks like a direct image file,
    or if resolution is skipped/fails for any reason. Otherwise fetches the
    page and extracts its og:image / twitter:image meta tag content.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        return url  # not fetchable — let the frontend fallback handle it
    if any(blocked in url.lower() for blocked in _BLOCKED_IMAGE_HOSTS):
        return url  # basic SSRF hygiene — never fetch internal/local addresses

    lower_no_query = url.split("?")[0].lower()
    if lower_no_query.endswith(_DIRECT_IMAGE_EXTENSIONS):
        return url  # already a direct image link — nothing to resolve

    if url in _resolved_image_url_cache:
        return _resolved_image_url_cache[url]

    resolved = url  # safe default: original URL, unresolved
    try:
        import httpx
        import re as _re_img
        import html as _html_img
        headers = {"User-Agent": "Mozilla/5.0 (compatible; GINIBaliBot/1.0; +https://ginibali.com)"}
        async with httpx.AsyncClient(timeout=4.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                body = resp.text[:200_000]  # meta tags always live in <head>
                match = (
                    _re_img.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', body)
                    or _re_img.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', body)
                    or _re_img.search(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']', body)
                    or _re_img.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']', body)
                )
                if match:
                    candidate = _html_img.unescape(match.group(1)).strip()
                    if candidate.startswith(("http://", "https://")):
                        resolved = candidate
    except Exception as _img_err:
        logger.warning(f"Image URL resolution failed for {url[:80]!r} (non-fatal): {_img_err}")

    _resolved_image_url_cache[url] = resolved
    return resolved


def _dnp_active_df():
    """Return a filtered DataFrame of active, date-valid DNP rows, or None."""
    df = cache.get("dnp_df")
    if df is None or df.empty:
        return None

    df = df.copy()
    df.columns = [c.strip() for c in df.columns]

    # Require non-empty Category and Sub-category
    for col in ("Category", "Sub-category"):
        if col in df.columns:
            df = df[df[col].astype(str).str.strip().ne("").fillna(False)]

    if df.empty:
        return None

    # Filter by Active flag
    if "Active" in df.columns:
        df = df[df["Active"].astype(str).str.strip().str.upper().isin(["TRUE", "YES", "1"])]

    if df.empty:
        return None

    # Filter by date range (missing/unparseable dates mean no restriction)
    today = datetime.now().date()
    if "Start Date" in df.columns:
        start = pd.to_datetime(df["Start Date"], errors="coerce", dayfirst=False).dt.date
        df = df[start.isna() | (start <= today)]
    if "End Date" in df.columns:
        end = pd.to_datetime(df["End Date"], errors="coerce", dayfirst=False).dt.date
        df = df[end.isna() | (end >= today)]

    if df.empty:
        return None

    # Sort by Priority (numeric ascending; non-numeric treated as 999)
    if "Priority" in df.columns:
        df = df.copy()
        df["_pri"] = pd.to_numeric(df["Priority"].astype(str).str.strip(), errors="coerce").fillna(999)
        df = df.sort_values("_pri").drop(columns=["_pri"])

    return df


async def get_dnp_categories() -> list:
    """Return distinct active DNP categories in priority order."""
    df = _dnp_active_df()
    if df is None or "Category" not in df.columns:
        return []
    seen: set = set()
    result = []
    for val in df["Category"]:
        v = str(val).strip()
        if v and v not in seen:
            seen.add(v)
            result.append(v)
    return result


async def get_dnp_subcategories(category: str) -> list:
    """Return ALL active promo rows for a DNP category.

    Each row is a separate entry even if the sub-category name repeats
    (e.g. two La Favela promos). Rows are identified by their sheet ID (DP001...).
    """
    df = _dnp_active_df()
    if df is None or "Sub-category" not in df.columns:
        return []
    filtered = df[df["Category"].astype(str).str.strip() == category.strip()]
    if filtered.empty:
        return []

    def _col(row, name, default=""):
        if name in row.index:
            val = str(row[name]).strip()
            return "" if val.lower() in ("nan", "") else val
        return default

    result = []
    for _, row in filtered.iterrows():
        sub = _col(row, "Sub-category")
        if sub:
            result.append({
                "id":          _col(row, "ID"),
                "name":        sub,
                "title":       _col(row, "Title"),
                "promo_type":  _col(row, "Promo Type"),
                "image_url":   await resolve_display_image_url(_col(row, "Image URL")),
                "description": _col(row, "Description"),
            })
    return result


async def get_dnp_promo(promo_id: str) -> dict | None:
    """Return the full promo card for a specific row ID (e.g. DP001).

    Canonical fields are always present with fixed snake_case keys.
    Any *additional* columns added to the D&P sheet are included automatically
    so new information appears without code changes (dynamic pass).

    Always excluded (internal operational data, never for guests):
      - Commission Value / Commission Type (financial split)
      - Partner Contact Name / Partner Contact WhatsApp (internal coordination)
    """
    _EXCLUDED: frozenset = frozenset({
        "Commission Value", "Commission Type",
        "Partner Contact Name", "Partner Contact WhatsApp",
        "Partner Contact (WhatsApp)", "Partner WhatsApp",
    })
    # Columns already mapped to canonical keys — skip in the dynamic pass.
    _CANONICAL_SHEET_COLS: frozenset = frozenset({
        "ID", "Category", "Sub-category", "Title", "Description",
        "Promo Type", "Referral Code", "Redemption Method",
        "Endpoints (Message)", "Image URL", "Start Date", "End Date",
        "Priority", "Status", "Active",
    })

    df = _dnp_active_df()
    if df is None or "ID" not in df.columns:
        return None
    filtered = df[df["ID"].astype(str).str.strip() == promo_id.strip()]
    if filtered.empty:
        return None

    row = filtered.iloc[0]

    def _get(col):
        if col in row.index:
            val = str(row[col]).strip()
            return "" if val.lower() in ("nan", "") else val
        return ""

    result = {
        "id":                _get("ID"),
        "category":          _get("Category"),
        "subcategory":       _get("Sub-category"),
        "title":             _get("Title"),
        "description":       _get("Description"),
        "promo_type":        _get("Promo Type"),
        "referral_code":     _get("Referral Code"),
        "redemption_method": _get("Redemption Method"),
        "endpoint":          _get("Endpoints (Message)"),
        "image_url":         await resolve_display_image_url(_get("Image URL")),
        "start_date":        _get("Start Date"),
        "end_date":          _get("End Date"),
    }

    # Dynamic pass: any new sheet column not in the canonical or excluded set
    # is added automatically so the promo detail view reflects the latest sheet.
    for col in row.index:
        col_str = str(col).strip()
        if col_str in _EXCLUDED or col_str in _CANONICAL_SHEET_COLS:
            continue
        val = _get(col_str)
        if not val:
            continue
        key = col_str.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_").replace("-", "_")
        if key not in result:
            result[key] = val

    return result


async def get_dnp_context() -> str:
    """
    Return a formatted, plain-text summary of ACTIVE Discounts & Promotions
    (live D&P sheet — same source as get_dnp_categories/get_dnp_subcategories)
    for AI grounding.

    Root cause fixed (2026-08-15): the general AI chat had zero visibility
    into the live D&P catalog when a guest asked about discounts/promotions
    via free text — get_dnp_categories/get_dnp_subcategories were only ever
    called from the WhatsApp tap-driven DNP Flow. The AI would guess and
    incorrectly claim "I don't have specific information on any current
    discounts" even when real, active promotions existed — reproduced live
    (Adam/Clay, 2026-08-15). Mirrors get_service_catalog_context's pattern:
    live sheet data only, never invented, "" on any failure (non-fatal).

    Excludes Commission Value/Type and Partner Contact fields — same
    guest-facing exclusion rule as get_dnp_promo.
    """
    try:
        df = _dnp_active_df()
        if df is None or df.empty or "Category" not in df.columns:
            return ""

        def _col(row, name):
            if name in row.index:
                val = str(row[name]).strip()
                return "" if val.lower() in ("nan", "") else val
            return ""

        catalog: dict = {}  # category -> list of "Sub-category — Title: Description"
        for _, row in df.iterrows():
            category = _col(row, "Category")
            sub = _col(row, "Sub-category")
            if not category or not sub:
                continue
            title = _col(row, "Title")
            desc = _col(row, "Description")
            label = f"{sub}"
            if title:
                label += f" — {title}"
            if desc:
                label += f": {desc}"
            catalog.setdefault(category, []).append(label)

        if not catalog:
            return ""

        lines = [
            "ACTIVE DISCOUNTS & PROMOTIONS (live Google Sheets D&P catalog — "
            "ONLY mention promotions from this list, never invent or assume "
            "there are none if this list is non-empty). Direct the guest to "
            "tap 'Discounts & Promotions' in the sidebar menu for full details "
            "and redemption steps:"
        ]
        for cat, items in catalog.items():
            lines.append(f"\n{cat}:")
            for item in items:
                lines.append(f"  • {item}")
        return "\n".join(lines)

    except Exception as _e:
        logger.warning(f"get_dnp_context failed (non-fatal): {_e}")
        return ""
