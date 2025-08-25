# auth_router.py
import json
from typing import Optional, Dict, List
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from app_utils import templates, NAV_ITEMS
from data.db import get_conn

router = APIRouter(tags=["Authentication"])

def _flatten_permission_keys(items: List[Dict]) -> List[str]:
    s = set()
    for it in items:
        k = it.get("permission_key")
        if k: s.add(k)
        for sub in it.get("sub_items", []) or []:
            sk = sub.get("permission_key")
            if sk: s.add(sk)
    return sorted(s)

PERMISSION_COLUMNS = _flatten_permission_keys(NAV_ITEMS)

def verify_user(username: str, password: str) -> Optional[Dict]:
    with get_conn() as c:
        u = c.execute("""
            SELECT id, loginname, display_name, role, status
            FROM users
            WHERE loginname=? AND password=? AND status=1
        """, (username, password)).fetchone()
        if not u: return None
        rows = c.execute("SELECT perm_key,value FROM user_permissions WHERE user_id=?", (u["id"],)).fetchall()
        perms = {k: False for k in PERMISSION_COLUMNS}
        for r in rows:
            if r["perm_key"] in perms:
                perms[r["perm_key"]] = bool(r["value"])
        return {
            "loginname": u["loginname"],
            "role": u["role"],
            "display_name": u["display_name"],
            "permissions": perms,
        }

@router.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    data = verify_user(username, password)
    if not data:
        return templates.TemplateResponse("login.html",
            {"request": request, "error": "帳號或密碼錯誤，或帳號已被停用。"})
    resp = RedirectResponse(url="/dashboard", status_code=302)
    cookie_opts = dict(httponly=True, samesite="lax")  # 若走 HTTPS 再加 secure=True
    resp.set_cookie("user", data["loginname"], **cookie_opts)
    resp.set_cookie("role", data["role"], **cookie_opts)
    resp.set_cookie("permissions", json.dumps(data["permissions"]), **cookie_opts)
    return resp

@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=302)
    for k in ("user","role","permissions"):
        resp.delete_cookie(k)
    return resp
