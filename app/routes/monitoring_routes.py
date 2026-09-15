# RECONSTRUCTED (sandbox) dev scaffold — system health. Reports which integrations
# are configured (real info), returns empty error/test lists.
from fastapi import APIRouter
from app.settings.config import settings
router = APIRouter(prefix="/health", tags=["monitoring_routes"])
def ok(**kw):
    d = {"success": True}; d.update(kw); return d
def _svc():
    return {
        "mongodb": bool(settings.MONGO_URII and "localhost" not in settings.MONGO_URII),
        "google_sheets": bool(settings.GOOGLE_SERVICE_ACCOUNT_JSON),
        "openai": bool(settings.OPENAI_API_KEY),
        "pinecone": bool(settings.pinecone_api_key),
        "xendit": bool(settings.XENDIT_SECRET_KEY),
        "whatsapp": bool(settings.access_token),
        "aws_s3": bool(settings.AWS_ACCESS_KEY),
    }
@router.get("")
async def health():
    return ok(status="ok", services=_svc())
@router.get("/dependencies")
async def deps():
    return ok(dependencies=[{"name": k, "configured": v, "status": "ok" if v else "not_configured"} for k, v in _svc().items()])
@router.get("/errors")
async def errors():
    return ok(errors=[])
@router.get("/errors-summary")
async def errors_summary():
    return ok(summary={"total": 0, "unresolved": 0})
@router.post("/errors/{error_id}/resolve")
async def resolve(error_id: str):
    return ok()
@router.get("/module/{name}")
async def module(name: str):
    return ok(module={"name": name, "status": "ok"})
@router.get("/test-results")
async def test_results():
    return ok(results=[])
@router.get("/test-results/{suite}")
async def suite(suite: str):
    return ok(result={"suite": suite, "tests": []})
