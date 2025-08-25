# permissions_router.py
from typing import Optional, Dict, List
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
import json

from app_utils import templates, NAV_ITEMS, get_base_context
from data.db import get_conn, upsert_permission_keys  # ✅ 改成 data.db

router = APIRouter(prefix="/admin/permissions", tags=["Permissions"])


def _need_admin(role: Optional[str]) -> bool:
    return role == "admin"


def _flatten_permission_keys(items: List[Dict]) -> List[str]:
    keys = set()
    for it in items:
        k = it.get("permission_key")
        if k:
            keys.add(k)
        for sub in it.get("sub_items", []) or []:
            sk = sub.get("permission_key")
            if sk:
                keys.add(sk)
    return sorted(keys)


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    """權限管理首頁（列表 + 勾選權限 + 角色/狀態）"""
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    perm_keys = _flatten_permission_keys(NAV_ITEMS)
    upsert_permission_keys(perm_keys)  # 確保權限鍵有同步到 DB

    with get_conn() as c:
        users = c.execute(
            """
            SELECT id, loginname, display_name, role, status
            FROM users
            ORDER BY loginname
            """
        ).fetchall()

        data = []
        for u in users:
            rows = c.execute(
                "SELECT perm_key, value FROM user_permissions WHERE user_id=?",
                (u["id"],),
            ).fetchall()
            perms = {k: 0 for k in perm_keys}
            for r in rows:
                if r["perm_key"] in perms:
                    perms[r["perm_key"]] = int(r["value"])
            data.append({"u": u, "perms": perms})

    ctx = get_base_context(request, user, role, permissions)
    ctx.update({"users": data, "perm_keys": perm_keys})
    return templates.TemplateResponse("admin/permissions.html", ctx)


@router.post("/update")
async def update_permissions(
    request: Request,
    user_id: int = Form(...),
    loginname: str = Form(...),
    role_new: str = Form(...),
    status_new: int = Form(...),
    perms_json: str = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    """
    更新單一使用者的角色/狀態/權限：
    - role_new: admin/user/guest
    - status_new: 1=啟用, 0=停用
    - perms_json: {"report":1,"events":0,...}
    """
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    try:
        perms: Dict[str, int] = {k: int(v) for k, v in json.loads(perms_json).items()}
    except Exception:
        perms = {}

    with get_conn() as c:
        # 更新 users
        c.execute(
            """
            UPDATE users
            SET role=?, status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (role_new, int(status_new), user_id),
        )

        # 更新 user_permissions
        for k, v in perms.items():
            c.execute(
                """
                INSERT INTO user_permissions(user_id, perm_key, value)
                VALUES (?,?,?)
                ON CONFLICT(user_id,perm_key)
                DO UPDATE SET value=excluded.value
                """,
                (user_id, k, int(v)),
            )

    return RedirectResponse(url="/admin/permissions?ok=1", status_code=303)


@router.post("/disable")
async def disable_user(
    request: Request,
    user_id: int = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    """一鍵停用帳號（status=0）"""
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    with get_conn() as c:
        c.execute(
            "UPDATE users SET status=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (user_id,),
        )
    return RedirectResponse(url="/admin/permissions?disabled=1", status_code=303)
