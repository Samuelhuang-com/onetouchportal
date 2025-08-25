# routers/debug_router.py
from typing import Optional, Dict
import os
from fastapi import APIRouter, Request, Cookie, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from app_utils import get_base_context, templates
from data.db import get_conn, DB_PATH

router = APIRouter(prefix="/debug", tags=["Debug"])

def _load_db_overview() -> Dict:
    path = os.path.abspath(DB_PATH)
    exists = os.path.exists(path)
    size = os.path.getsize(path) if exists else 0
    with get_conn() as c:
        try:
            users_cnt = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        except Exception:
            users_cnt = "N/A"
        try:
            sample = c.execute("""
                SELECT id, loginname, role, status, updated_at
                FROM users
                ORDER BY id
                LIMIT 10
            """).fetchall()
            sample_rows = [dict(r) for r in sample]
        except Exception:
            sample_rows = []
    return {
        "db_path": path,
        "db_exists": exists,
        "db_size": size,
        "users_cnt": users_cnt,
        "sample_rows": sample_rows,
    }

def _query_user_with_permissions(loginname: str) -> Optional[Dict]:
    """回傳 {user: {...}, permissions: {key: int}, enabled_count: int, all_keys: [..]}"""
    if not loginname:
        return None
    with get_conn() as c:
        u = c.execute("""
            SELECT id, loginname, display_name, role, status, updated_at
            FROM users
            WHERE loginname=?
        """, (loginname,)).fetchone()
        if not u:
            return None
        uid = u["id"]
        # 全部權限鍵（為了完整顯示，沒設定的也列出）
        all_keys_rows = c.execute("SELECT key FROM permission_keys ORDER BY key").fetchall()
        all_keys = [r["key"] for r in all_keys_rows]
        # 使用者已設定的權限
        rows = c.execute("""
            SELECT perm_key, value
            FROM user_permissions
            WHERE user_id=?
        """, (uid,)).fetchall()
        perms = {k: 0 for k in all_keys}
        for r in rows:
            if r["perm_key"] in perms:
                perms[r["perm_key"]] = int(r["value"])
        enabled = sum(1 for v in perms.values() if v == 1)
        return {
            "user": dict(u),
            "permissions": perms,
            "enabled_count": enabled,
            "all_keys": all_keys,
        }

@router.get("/db-check", response_class=HTMLResponse)
async def db_check_get(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    q: Optional[str] = None,  # 允許用 querystring ?q=loginname
):
    if not user:
        return RedirectResponse(url="/login")

    # 非 admin 僅允許查自己
    if q and role != "admin" and q != user:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    overview = _load_db_overview()
    query_result = _query_user_with_permissions(q) if q else None

    ctx = get_base_context(request, user, role, permissions)
    ctx.update(overview)
    ctx.update({"query_login": q or "", "query_result": query_result})
    return templates.TemplateResponse("debug/db_check.html", ctx)

@router.post("/db-check", response_class=HTMLResponse)
async def db_check_post(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    q: str = Form(""),
):
    if not user:
        return RedirectResponse(url="/login")
    q = (q or "").strip()
    # 非 admin 僅允許查自己
    if q and role != "admin" and q != user:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    overview = _load_db_overview()
    query_result = _query_user_with_permissions(q) if q else None

    ctx = get_base_context(request, user, role, permissions)
    ctx.update(overview)
    ctx.update({"query_login": q, "query_result": query_result})
    return templates.TemplateResponse("debug/db_check.html", ctx)
