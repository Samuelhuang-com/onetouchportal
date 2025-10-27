# routers/events_router.py
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, Cookie, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi import Form, Body
from app_utils import templates, get_base_context
from data.perm_service import has_permission as has_perm_db
from data.events_service import (
    list_events,
    insert_event,
    get_event,
    update_event,
    delete_event,
)

router = APIRouter(tags=["Events"])


# 簡易權限檢查（避免循環 import，不直接從 main 引用）
def require_events_perm(
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    user: Optional[str] = Cookie(None),
) -> bool:
    if role == "admin":
        return True
    # 後端再從 DB 驗證一次，避免 cookie 被竄改
    return has_perm_db(user or "", "events")


@router.get("/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    ok: bool = Depends(require_events_perm),
):
    if not user:
        return RedirectResponse(url="/")
    if not ok:
        return RedirectResponse(url="/dashboard?error=permission_denied")
    ctx = get_base_context(request, user, role, permissions)
    # 前端 AG-Grid 直接呼叫 /api/events 取資料，模板內不再塞入 events 陣列
    return templates.TemplateResponse("events.html", ctx)


@router.get("/events/add", response_class=HTMLResponse)
async def add_event_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    ok: bool = Depends(require_events_perm),
):
    if not user:
        return RedirectResponse(url="/")
    if not ok:
        return RedirectResponse(url="/dashboard?error=permission_denied")
    ctx = get_base_context(request, user, role, permissions)
    return templates.TemplateResponse("add_event.html", ctx)


@router.post("/events/add")
async def add_event_submit(
    request: Request,
    日期: str = Form(...),
    活動名稱: str = Form(...),
    負責人: str = Form(...),
    地點: str = Form(...),
    活動截止日: Optional[str] = Form(None),
    網址: Optional[str] = Form(None),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    ok: bool = Depends(require_events_perm),
):
    if not user:
        return RedirectResponse(url="/")
    if not ok:
        return RedirectResponse(url="/dashboard?error=permission_denied")
    insert_event(
        date=日期.strip(),
        name=活動名稱.strip(),
        owner=負責人.strip(),
        location=地點.strip(),
        deadline=(活動截止日 or "").strip() or None,
        url=(網址 or "").strip() or None,
    )
    return RedirectResponse(url="/events", status_code=302)


# === JSON APIs for AG-Grid ===


@router.get("/api/events")
async def api_list_events(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    ok: bool = Depends(require_events_perm),
):
    if not user or not ok:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return JSONResponse(list_events())


@router.put("/api/events/{eid}")
async def api_update_event(
    eid: int,
    payload: Dict[str, Any] = Body(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    ok: bool = Depends(require_events_perm),
):
    if not user or not ok:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    # 接收中文鍵名，轉成 DB 欄位名
    mapping = {
        "日期": "date",
        "活動名稱": "name",
        "負責人": "owner",
        "地點": "location",
        "活動截止日": "deadline",
        "網址": "url",
    }
    to_update = {}
    for k, v in payload.items():
        if k in mapping:
            to_update[mapping[k]] = (v or "").strip()
    if not to_update:
        return JSONResponse({"updated": 0})
    n = update_event(eid, **to_update)
    return JSONResponse({"updated": n})


@router.delete("/api/events/{eid}")
async def api_delete_event(
    eid: int,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    ok: bool = Depends(require_events_perm),
):
    if not user or not ok:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    n = delete_event(eid)
    return JSONResponse({"deleted": n})
