from fastapi import APIRouter, Request
router = APIRouter(prefix="/issues", tags=["issue_routes"])
@router.get("/notifications/{guest_id}")
async def notifications(guest_id: str): return {"notifications": []}
@router.post("/submit")
async def submit(request: Request):
    # accept multipart OR json (issue reports include an optional image upload)
    return {"success": True, "message": "Maintenance issue submitted. The villa team has been notified.", "issue_id": "ISS-DEV-1"}
