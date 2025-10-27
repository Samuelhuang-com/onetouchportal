from typing import Optional, Dict, List
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import json
from datetime import datetime
import os
import sqlite3  # ← 新增：連到 auth.db

from app_utils import templates, NAV_ITEMS, get_base_context
from data.db import get_conn, upsert_permission_keys

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


# === 你可自由擴充的額外權限鍵（不一定出現在 NAV，但希望出現在畫面） ===
EXTRA_KEYS: set[str] = {
    "complaints_manage",
}

# === 讀寫 auth.db → permissions_keys(key) =====================
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "auth.db")  # 若路徑不同，用環境變數覆蓋


def _auth_conn():
    conn = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _load_perm_keys_from_auth() -> list[str]:
    with _auth_conn() as a:
        a.execute("CREATE TABLE IF NOT EXISTS permissions_keys (key TEXT PRIMARY KEY)")
        rows = a.execute("SELECT key FROM permissions_keys").fetchall()
    return [r["key"] for r in rows]


def _ensure_perm_keys_in_auth(keys: list[str]) -> None:
    if not keys:
        return
    with _auth_conn() as a:
        a.execute("CREATE TABLE IF NOT EXISTS permissions_keys (key TEXT PRIMARY KEY)")
        a.executemany(
            "INSERT OR IGNORE INTO permissions_keys(key) VALUES (?)",
            [(k,) for k in keys],
        )


# ============================== Pages =========================================


@router.get("", response_class=HTMLResponse)
async def page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    # ⭐ 核心修正：以 DB + NAV + EXTRA 聯集為權限欄位來源
    db_keys = _load_perm_keys_from_auth()  # auth.db → permissions_keys(key)
    nav_keys = _flatten_permission_keys(NAV_ITEMS)  # 原本就有的 NAV 鍵
    perm_keys = sorted(set(db_keys) | set(nav_keys) | EXTRA_KEYS)

    # 把缺的鍵補回 auth.db；同時保留原本對舊表的 upsert（若你的專案需要）
    _ensure_perm_keys_in_auth(perm_keys)
    try:
        upsert_permission_keys(perm_keys)
    except Exception:
        pass  # 若另一邊的表結構不同，就不擋頁面

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
            # 預設為未勾選（0），確保每個鍵都有欄位
            perms = {k: 0 for k in perm_keys}
            for r in rows:
                if r["perm_key"] in perms:
                    perms[r["perm_key"]] = int(r["value"])
            data.append({"u": u, "perms": perms})

    ctx = get_base_context(request, user, role, permissions)
    ctx.update({"users": data, "perm_keys": perm_keys})
    return templates.TemplateResponse("admin/permissions.html", ctx)


# ============================== Update / Disable ==============================


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
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    try:
        perms: Dict[str, int] = {k: int(v) for k, v in json.loads(perms_json).items()}
    except Exception:
        perms = {}

    with get_conn() as c:
        c.execute(
            """
            UPDATE users
            SET role=?, status=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (role_new, int(status_new), user_id),
        )
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

    is_xhr = request.headers.get("x-requested-with", "").lower() in (
        "fetch",
        "xmlhttprequest",
    )
    if is_xhr:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/admin/permissions?ok=1", status_code=303)


@router.post("/disable")
async def disable_user(
    request: Request,
    user_id: int = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    if not user or not _need_admin(role):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    with get_conn() as c:
        c.execute(
            "UPDATE users SET status=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (user_id,),
        )

    is_xhr = request.headers.get("x-requested-with", "").lower() in (
        "fetch",
        "xmlhttprequest",
    )
    if is_xhr:
        return JSONResponse({"ok": True})
    return RedirectResponse(url="/admin/permissions?disabled=1", status_code=303)


# （以下若與 contracts 附件無關，可保留或移除）
CONTRACT_ATTACHMENT_DIR = os.path.join("data", "contracts", "attachments")


def get_attachment_meta(contract_id: str):
    os.makedirs(CONTRACT_ATTACHMENT_DIR, exist_ok=True)
    for filename in os.listdir(CONTRACT_ATTACHMENT_DIR):
        if filename.startswith(contract_id):
            path = os.path.join(CONTRACT_ATTACHMENT_DIR, filename)
            try:
                mtime = os.path.getmtime(path)
                mtime_iso = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
            except Exception:
                mtime_iso = None
            return {"filename": filename, "mtime_iso": mtime_iso}
    return {"filename": None, "mtime_iso": None}
