# permissions_router.py
from typing import Optional, Dict, List
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
import json
from app_utils import templates, NAV_ITEMS, get_base_context
from data.db import get_conn, upsert_permission_keys, audit_log

router = APIRouter(prefix="/admin/permissions", tags=["Permissions"])

def _need_admin(role: Optional[str]) -> bool: return role == "admin"

def _perm_keys() -> List[str]:
    s = set()
    for it in NAV_ITEMS:
        k = it.get("permission_key")
        if k: s.add(k)
        for sub in it.get("sub_items", []) or []:
            sk = sub.get("permission_key")
            if sk: s.add(sk)
    keys = sorted(s)
    upsert_permission_keys(keys)
    return keys

@router.get("", response_class=HTMLResponse)
async def page(request: Request,
               user: Optional[str] = Cookie(None),
               role: Optional[str] = Cookie(None),
               permissions: Optional[str] = Cookie(None)):
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")
    keys = _perm_keys()
    with get_conn() as c:
        users = c.execute("SELECT id,loginname,display_name,role,status FROM users ORDER BY loginname").fetchall()
        data = []
        for u in users:
            rows = c.execute("SELECT perm_key,value FROM user_permissions WHERE user_id=?", (u["id"],)).fetchall()
            perms = {k:0 for k in keys}
            for r in rows:
                if r["perm_key"] in perms: perms[r["perm_key"]] = int(r["value"])
            data.append({"u": u, "perms": perms})
    ctx = get_base_context(request, user, role, permissions)
    ctx.update({"users": data, "perm_keys": keys})
    return templates.TemplateResponse("admin/permissions.html", ctx)

@router.post("/update")
async def update_permissions(request: Request,
    user_id: int = Form(...),
    loginname: str = Form(...),
    role_new: str = Form(...),
    status_new: int = Form(...),
    perms_json: str = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None)
):
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")
    try:
        perms: Dict[str,int] = {k:int(v) for k,v in json.loads(perms_json).items()}
    except Exception:
        perms = {}
    with get_conn() as c:
        c.execute("UPDATE users SET role=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                  (role_new, int(status_new), user_id))
        for k,v in perms.items():
            c.execute("""
            INSERT INTO user_permissions(user_id,perm_key,value)
            VALUES (?,?,?)
            ON CONFLICT(user_id,perm_key) DO UPDATE SET value=excluded.value
            """, (user_id, k, int(v)))
    audit_log(actor=user or "", action="UPDATE_USER_PERMS",
              target=f"user:{loginname}",
              details={"role":role_new, "status":int(status_new), "perms":perms})
    return RedirectResponse(url="/admin/permissions?ok=1", status_code=303)

@router.post("/disable")
async def disable_user(request: Request,
    user_id: int = Form(...),
    loginname: str = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None)):
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")
    with get_conn() as c:
        c.execute("UPDATE users SET status=0, updated_at=CURRENT_TIMESTAMP WHERE id=?", (user_id,))
    audit_log(actor=user or "", action="DISABLE_USER", target=f"user:{loginname}", details={})
    return RedirectResponse(url="/admin/permissions?disabled=1", status_code=303)
