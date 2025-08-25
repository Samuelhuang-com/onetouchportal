# roles_router.py
from typing import Optional, Dict, List
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
import json
from app_utils import templates, get_base_context, NAV_ITEMS
from data.db import get_conn, upsert_permission_keys, audit_log

router = APIRouter(prefix="/admin/roles", tags=["Roles"])

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
async def list_roles(request: Request,
                     user: Optional[str] = Cookie(None),
                     role: Optional[str] = Cookie(None),
                     permissions: Optional[str] = Cookie(None)):
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")
    keys = _perm_keys()
    with get_conn() as c:
        roles = c.execute("SELECT id,name,display_name FROM roles ORDER BY name").fetchall()
        data=[]
        for r in roles:
            rows = c.execute("SELECT perm_key,value FROM role_permissions WHERE role_id=?", (r["id"],)).fetchall()
            perms = {k:0 for k in keys}
            for rr in rows:
                if rr["perm_key"] in perms: perms[rr["perm_key"]] = int(rr["value"])
            data.append({"role": r, "perms": perms})
    ctx = get_base_context(request, user, role, permissions)
    ctx.update({"roles": data, "perm_keys": keys})
    return templates.TemplateResponse("admin/roles.html", ctx)

@router.post("/add")
async def add_role(request: Request,
                   name: str = Form(...),
                   display_name: str = Form(""),
                   user: Optional[str] = Cookie(None),
                   role: Optional[str] = Cookie(None)):
    if not user or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO roles(name,display_name) VALUES(?,?)", (name.strip(), display_name.strip()))
    audit_log(user or "", "ADD_ROLE", f"role:{name}", {"display_name":display_name})
    return RedirectResponse(url="/admin/roles", status_code=303)

@router.post("/delete")
async def delete_role(request: Request,
                      role_id: int = Form(...),
                      role_name: str = Form(""),
                      user: Optional[str] = Cookie(None),
                      role: Optional[str] = Cookie(None)):
    if not user or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")
    with get_conn() as c:
        c.execute("DELETE FROM roles WHERE id=?", (role_id,))
    audit_log(user or "", "DELETE_ROLE", f"role:{role_name}", {})
    return RedirectResponse(url="/admin/roles", status_code=303)

@router.post("/update_perms")
async def update_role_perms(request: Request,
                            role_id: int = Form(...),
                            role_name: str = Form(""),
                            perms_json: str = Form(...),
                            user: Optional[str] = Cookie(None),
                            role: Optional[str] = Cookie(None)):
    if not user or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")
    try:
        perms: Dict[str,int] = {k:int(v) for k,v in json.loads(perms_json).items()}
    except Exception:
        perms = {}
    with get_conn() as c:
        for k,v in perms.items():
            c.execute("""
            INSERT INTO role_permissions(role_id,perm_key,value)
            VALUES (?,?,?)
            ON CONFLICT(role_id,perm_key) DO UPDATE SET value=excluded.value
            """, (role_id, k, int(v)))
    audit_log(user or "", "UPDATE_ROLE_PERMS", f"role:{role_name}", {"perms":perms})
    return RedirectResponse(url="/admin/roles?ok=1", status_code=303)
