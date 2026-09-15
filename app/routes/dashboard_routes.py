# RECONSTRUCTED (sandbox) dev scaffold — returns success-shaped EMPTY data so the
# admin dashboard is fully navigable without a database. Real data appears once
# MongoDB is connected and the original route logic is restored.
from fastapi import APIRouter, Request

router = APIRouter(prefix="/dashboard-api", tags=["dashboard_routes"])


def ok(**kw):
    d = {"success": True}
    d.update(kw)
    return d


_STATS = {
    "activeGuests": 0, "registeredProfiles": 0, "totalBookings": 0,
    "revenue": 0, "reportedIssues": 0,
    "paymentLifecycle": {"accepted": 0, "waiting": 0, "completed": 0, "disbursed": 0, "refunded": 0},
}


@router.get("/stats")
async def stats():
    return ok(stats=_STATS, recentActivity=[])


@router.get("/bookings")
async def bookings(request: Request):
    return ok(bookings=[], total=0, page=1)


@router.get("/status-options/{order_number}")
async def status_options(order_number: str):
    return ok(current_status=None, options=[])


@router.get("/orders/{order_number}/full-view")
async def order_full_view(order_number: str):
    return ok(order={}, guest={}, service={}, payment={}, disbursement={}, service_provider={})


@router.get("/guest/{guest_id}/full-view")
async def guest_full_view(guest_id: str):
    return ok(guest={}, bookings=[])


@router.get("/villa/list")
async def villa_list():
    return ok(villas=[])


@router.get("/villa/profile")
async def villa_profile(request: Request):
    return ok(villa={})


@router.post("/villa/profile")
async def save_villa_profile(request: Request):
    return ok(message="Saved (dev mode).")


@router.get("/villa/{villa_code}/full-view")
async def villa_full_view(villa_code: str):
    return ok(villa={}, bookings=[])


@router.get("/sp/{sp_code}/full-view")
async def sp_full_view(sp_code: str):
    return ok(service_provider={}, bookings=[])


@router.get("/payment/{invoice_id}")
async def payment_view(invoice_id: str):
    return ok(payment={})


@router.get("/disbursement/{order_number}")
async def disbursement_view(order_number: str):
    return ok(disbursement={})


@router.post("/orders/{order_number}/status-transition")
async def status_transition(order_number: str, request: Request):
    return ok(message="Status updated (dev mode — not persisted).")


@router.get("/buckets/{bucket}")
async def buckets(bucket: str):
    return ok(items=[], data=[], total=0)


@router.get("/arrivals/expected")
async def arrivals():
    return ok(arrivals=[])


@router.post("/arrivals/expected")
async def add_arrival(request: Request):
    return ok(message="Added (dev mode).")


@router.delete("/arrivals/expected/{id}")
async def del_arrival(id: str):
    return ok()


@router.get("/checkins")
async def checkins():
    return ok(checkins=[])


@router.post("/checkins/{id}/keys-returned")
async def keys_returned(id: str):
    return ok()


@router.get("/customers/search")
async def customers_search(request: Request):
    return ok(customers=[])


@router.get("/chats")
async def chats():
    return ok(chats=[])


@router.get("/feedback")
async def feedback():
    return ok(feedback=[])


@router.get("/issues")
async def issues():
    return ok(issues=[])


@router.patch("/issues/{id}/status")
async def issue_status(id: str, request: Request):
    return ok()


@router.get("/notifications/health")
async def notif_health():
    return ok(health={"status": "ok"})


@router.get("/notifications/records")
async def notif_records():
    return ok(records=[])


@router.get("/partners")
async def partners():
    return ok(partners=[])


@router.delete("/partners/{id}")
async def del_partner(id: str):
    return ok()


@router.get("/passports")
async def passports():
    return ok(passports=[])


@router.put("/passports/{id}/verify")
async def verify_passport(id: str):
    return ok()


@router.put("/passports/{id}/reject")
async def reject_passport(id: str, request: Request):
    return ok()


@router.get("/refunds")
async def refunds():
    return ok(refunds=[])


@router.post("/refunds")
async def add_refund(request: Request):
    return ok()


@router.post("/refunds/{id}/approve")
async def approve_refund(id: str):
    return ok()


@router.post("/refunds/{id}/reject")
async def reject_refund(id: str, request: Request):
    return ok()


@router.get("/reports/summary")
async def reports_summary():
    return ok(summary={"guests": {"total": 0}, "revenue": {"total": 0}, "bookings": {"total": 0}})


@router.get("/reports/guests")
async def reports_guests():
    return ok(data={"total": 0, "series": []})


@router.get("/reports/revenue")
async def reports_revenue():
    return ok(data={"total": 0, "series": []})


@router.get("/explore/{type}/{id}")
async def explore(type: str, id: str):
    return ok(data={})


@router.get("/history/customer/{phone}")
async def customer_history(phone: str):
    return ok(history=[])


# generic catch-all for any dashboard-api GET the UI calls that we did not map
@router.get("/{endpoint:path}")
async def generic(endpoint: str):
    return ok(data=[], items=[])
