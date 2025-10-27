# routers/notes_router.py — Notes routes with debug support
from fastapi import APIRouter, Request, HTTPException, Form
from fastapi.responses import JSONResponse, HTMLResponse
from typing import Optional, List, Dict, Any
import json

from app_utils import templates, get_base_context, ensure_permission_pack
from data.perm_service import has_permission
from notes_module import (
    NotesManager,
    get_employee_by_login,
    get_departments_by_employee_id,
    debug_log,
)

router = APIRouter()
nm = NotesManager()
ensure_permission_pack("notes")


def _cookie_json(request: Request, key: str) -> dict:
    try:
        return json.loads(request.cookies.get(key) or "{}")
    except Exception:
        return {}


def _has_perm(request: Request, key: str) -> bool:
    perms = _cookie_json(request, "permissions")
    if perms.get(key):
        return True
    user = request.cookies.get("user") or ""
    return has_permission(user, key)


def _require_perm(request: Request, key: str):
    if not _has_perm(request, key):
        raise HTTPException(status_code=401, detail=f"permission required: {key}")


def _current_emp_info(request: Request) -> Dict[str, Any]:
    emp = (
        request.cookies.get("emp_id") or request.cookies.get("employee_id") or ""
    ).strip()
    user = (request.cookies.get("user") or "").strip()
    cookie_numeric_id = (request.cookies.get("id") or "").strip()  # 可能是 "23"

    def is_numeric(s: str) -> bool:
        return s.isdigit()

    candidate_emp = emp if emp else ""
    if (
        candidate_emp
        and not is_numeric(candidate_emp)
        and is_numeric(cookie_numeric_id)
    ):
        candidate_emp = cookie_numeric_id

    if candidate_emp:
        depts = get_departments_by_employee_id(candidate_emp) or []
        info = {"id": candidate_emp, "Departments": depts, "login": user}
        debug_log("current_emp_info by candidate_emp", **info)
        return info

    if user:
        info0 = get_employee_by_login(user)
        if info0.get("id"):
            info = {
                "id": info0["id"],
                "Departments": info0.get("Departments", []) or [],
                "login": user,
            }
            debug_log("current_emp_info by login map", **info)
            return info
        depts = get_departments_by_employee_id(user) or []
        info = {"id": user, "Departments": depts, "login": user}
        debug_log("current_emp_info fallback user=id", **info)
        return info

    debug_log("current_emp_info empty")
    return {"id": "", "Departments": [], "login": ""}


def _q_debug(request: Request) -> bool:
    v = request.query_params.get("debug")
    return str(v).lower() in ("1", "true", "yes", "y", "on")


@router.get("/api/notes/ping", include_in_schema=False)
async def api_notes_ping():
    return JSONResponse({"ok": True, "pong": nm.ping()})


@router.get("/api/notes/departments", include_in_schema=False)
async def api_notes_departments(request: Request):
    _require_perm(request, "notes_view")
    return JSONResponse({"departments": nm.departments_for_ui(), "department_1": []})


@router.post("/api/notes/send", include_in_schema=False)
async def api_notes_send(
    request: Request, content: str = Form(...), departments: str = Form(...)
):
    _require_perm(request, "notes_manage")
    me = _current_emp_info(request)
    sender = (me.get("id") or "").strip()
    if not sender:
        return JSONResponse(
            {
                "ok": False,
                "error_code": "NO_SENDER",
                "error_message": "發送者身份無法辨識",
            },
            status_code=400,
        )
    try:
        maybe = json.loads(departments)
        if isinstance(maybe, list):
            deplist = [str(x).strip() for x in maybe if str(x).strip()]
        else:
            deplist = [x.strip() for x in str(maybe).split(",") if x.strip()]
    except Exception:
        deplist = [x.strip() for x in departments.split(",") if x.strip()]
    debug_log("api_notes_send", sender=sender, depts=deplist)
    res = nm.send(sender_id=sender, departments=deplist, content=content)
    return JSONResponse(res, status_code=(200 if res.get("ok") else 400))


@router.post("/api/notes/{note_id}/read", include_in_schema=False)
async def api_notes_read(request: Request, note_id: int):
    _require_perm(request, "notes_view")
    me = _current_emp_info(request)
    emp_id = (me.get("id") or "").strip()
    my_depts: List[str] = me.get("Departments", []) or []
    login = (me.get("login") or "").strip()
    if not emp_id and not login:
        return JSONResponse(
            {"ok": False, "error_code": "NO_EMP", "error_message": "未辨識使用者"},
            status_code=400,
        )
    res = nm.mark_read(
        note_id,
        emp_id,
        employee_departments=my_depts,
        login=login,
        debug=_q_debug(request),
    )
    return JSONResponse(res, status_code=(200 if res.get("ok") else 403))


@router.get("/api/notes/{note_id}/reads", include_in_schema=False)
async def api_notes_reads(request: Request, note_id: int):
    _require_perm(request, "notes_view")
    data = nm.get_reads(note_id)
    return JSONResponse(data, status_code=200)


@router.get("/api/notes/unread-count", include_in_schema=False)
async def api_notes_unread(request: Request):
    _require_perm(request, "notes_view")
    me = _current_emp_info(request)
    emp_id = (me.get("id") or "").strip()
    if not emp_id:
        return JSONResponse({"ok": True, "unread": 0})
    return JSONResponse({"ok": True, "unread": nm.unread_count(emp_id)})


@router.get("/api/notes/list", include_in_schema=False)
async def api_notes_list(
    request: Request,
    sender_id: Optional[str] = None,
    department_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    _require_perm(request, "notes_view")
    me = _current_emp_info(request)
    emp_id = (me.get("id") or "").strip()
    my_depts: List[str] = me.get("Departments", []) or []
    if not emp_id:
        return JSONResponse(
            {"ok": False, "error_code": "NO_EMP", "error_message": "未辨識使用者"},
            status_code=400,
        )
    data = nm.list_notes(
        current_emp=emp_id,
        current_emp_departments=my_depts,
        page=page,
        page_size=page_size,
        sender_id=sender_id,
        department_id=department_id,
        include_sent_by_me=True,
        debug=_q_debug(request),
    )
    return JSONResponse(data)


@router.get("/notes", response_class=HTMLResponse, include_in_schema=False)
async def notes_page(request: Request):
    _require_perm(request, "notes_view")
    user = request.cookies.get("user")
    role = request.cookies.get("role")
    perms = request.cookies.get("permissions")
    ctx = get_base_context(request, user, role, perms)
    ctx.update(
        {
            "page_title": "Notes",
            "active_menu": "notes",
            "can_manage": _has_perm(request, "notes_manage"),
            "notes": [],
        }
    )
    return templates.TemplateResponse("notes/index.html", ctx)
