"""
villa_scope.py — Shared villa-scoping helper for dashboard endpoints.

Reads villa_codes from the authenticated user's JWT payload and returns
a MongoDB filter dict. Every dashboard query that should respect villa
isolation must merge this filter into its $match stage.

Rules:
    - admin (villa_codes=["*"]) → {} (no filter, sees everything)
    - staff (villa_codes=["V1","V2"]) → {"villa_code": {"$in": ["V1","V2"]}}
    - empty/missing villa_codes → treated as admin (backward-compat with
      legacy tokens issued before villa_codes was added)
"""

from typing import Dict, Any


def get_villa_scope(user: dict) -> Dict[str, Any]:
    """
    Returns a MongoDB filter dict for villa-scoped queries.

    Usage in endpoints:
        vs = get_villa_scope(user)
        await collection.count_documents({**vs, "status": "open"})
        pipeline = [{"$match": {**vs, "status": {"$in": PAID_STATUSES}}}, ...]
    """
    codes = user.get("villa_codes", ["*"])
    if not codes or "*" in codes:
        return {}
    return {"villa_code": {"$in": codes}}


def is_eb_admin(user: dict) -> bool:
    """True if the user has unrestricted (all-villa) access."""
    codes = user.get("villa_codes", ["*"])
    return not codes or "*" in codes
