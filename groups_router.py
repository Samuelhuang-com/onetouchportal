# groups_router.py
from typing import Optional, Dict, List
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
import json
from app_utils import templates, get_base_context, NAV_ITEMS
from data.db import get_conn, upsert_permission_keys, audit_log

router = APIRouter(prefix="/admin/groups", tags=["Groups"])

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
async def list_groups(request: Request,
                      user: Optional[str] = Cookie(None),
                      role: Optional[str] = Cookie(None),
                      permissions: Optional[str] = Cookie(None)):
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")
    keys = _perm_keys()
    with get_conn() as c:
        groups = c.execute("SELECT id,name,display_name FROM groups ORDER BY name").fetchall()
        data=[]
        for g in groups:
            rows = c.execute("SELECT perm_key,value FROM group_permissions WHERE group_id=?", (g["id"],)).fetchall()
            perms = {k:0 for k in keys}
            for rr in rows:
                if rr["perm_key"] in perms: perms[rr["perm_key"]] = int(rr["value"])
            members = c.execute("""
                SELECT u.id,u.loginname
                FROM users u JOIN group_members gm ON gm.user_id=u.id
                WHERE gm.group_id=? ORDER BY u.loginname
            """, (g["id"],)).fetchall()
            data.append({"group": g, "perms": perms, "members": members})
    ctx = get_base_context(request, user, role, permissions)
    ctx.update({"groups": data, "perm_keys": keys})
    return templates.TemplateResponse("admin/groups.html", ctx)

@router.post("/add")
async def add_group(request: Request,
                    name: str = Form(...),
                    display_name: str = Form(""),
                    user: Optional[str] = Cookie(None),
                    role: Optional[str] = Cookie(None)):
    if not user or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO groups(name,display_name) VALUES(?,?)", (name.strip(), display_name.strip()))
    audit_log(user or "", "ADD_GROUP", f"group:{name}", {"display_name":display_name})
    return RedirectResponse(url="/admin/groups", status_code=303)

@router.post("/delete")
async def delete_group(request: Request,
                       group_id: int = Form(...),
                       group_name: str = Form(""),
                       user: Optional[str] = Cookie(None),
                       role: Optional[str] = Cookie(None)):
    if not user or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")
    with get_conn() as c:
        c.execute("DELETE FROM groups WHERE id=?", (group_id,))
    audit_log(user or "", "DELETE_GROUP", f"group:{group_name}", {})
    return RedirectResponse(url="/admin/groups", status_code=303)

@router.post("/update_perms")
async def update_group_perms(request: Request,
                             group_id: int = Form(...),
                             group_name: str = Form(""),
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
            INSERT INTO group_permissions(group_id,perm_key,value)
            VALUES (?,?,?)
            ON CONFLICT(group_id,perm_key) DO UPDATE SET value=excluded.value
            """, (group_id, k, int(v)))
    audit_log(user or "", "UPDATE_GROUP_PERMS", f"group:{group_name}", {"perms":perms})
    return RedirectResponse(url="/admin/groups?ok=1", status_code=303)

@router.post("/add_member")
async def add_member(request: Request,
                     group_id: int = Form(...),
                     loginname: str = Form(...),
                     user: Optional[str] = Cookie(None),
                     role: Optional[str] = Cookie(None)):
    if not user or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")
    with get_conn() as c:
        u = c.execute("SELECT id FROM users WHERE loginname=?", (loginname.strip(),)).fetchone()
        if u:
            c.execute("INSERT OR IGNORE INTO group_members(group_id,user_id) VALUES(?,?)", (group_id, u["id"]))
            audit_log(user or "", "ADD_MEMBER", f"group:{group_id}", {"loginname":loginname})
    return RedirectResponse(url="/admin/groups?ok=1", status_code=303)

@router.post("/remove_member")
async def remove_member(request: Request,
                        group_id: int = Form(...),
                        user_id: int = Form(...),
                        user: Optional[str] = Cookie(None),
                        role: Optional[str] = Cookie(None)):
    if not user or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")
    with get_conn() as c:
        c.execute("DELETE FROM group_members WHERE group_id=? AND user_id=?", (group_id, user_id))
    audit_log(user or "", "REMOVE_MEMBER", f"group:{group_id}", {"user_id":user_id})
    return RedirectResponse(url="/admin/groups?ok=1", status_code=303)
