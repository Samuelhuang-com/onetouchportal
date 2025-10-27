from fastapi import APIRouter, Request, Cookie, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
from app_utils import templates, get_base_context
from data.session_service import list_active_sessions, revoke_session
import json

router = APIRouter(prefix="/admin", tags=["Admin"])

def is_admin(role: Optional[str]) -> bool:
    return role == "admin"

@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user or not is_admin(role):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    ctx = get_base_context(request, user, role, permissions)
    return templates.TemplateResponse("admin/sessions.html", ctx)

@router.get("/api/sessions")
async def sessions_list(role: Optional[str] = Cookie(None)):
    if not is_admin(role):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return {"items": list_active_sessions()}

@router.post("/api/sessions/{session_id}/revoke")
async def sessions_revoke(session_id: str, role: Optional[str] = Cookie(None)):
    if not is_admin(role):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    revoke_session(session_id)
    return {"ok": True}
