# routers/memos_router.py
from fastapi import APIRouter, Request, Cookie, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from typing import Optional, List, Dict, Any
from pathlib import Path
import sqlite3, os, json
from datetime import datetime, timezone

from app_utils import get_base_context, templates

# ========= DB =========
BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE_DIR / "data" / "approvals.db"  # 與 approvals 共用
DB_PATH = os.getenv("APPROVALS_DB", str(DEFAULT_DB))

# ✅ Router 定義
router = APIRouter(prefix="/memos", tags=["Memos"])

# ========= 小工具 =========
def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

def ensure_memos_table():
    """建立 memos 表"""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS memos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                body TEXT,
                visibility TEXT NOT NULL DEFAULT 'org',  -- org/restricted
                created_at TEXT NOT NULL,
                author TEXT,
                source TEXT,
                source_id INTEGER
            )
        """)
        conn.commit()

def can_use_portal(role: Optional[str], permissions: Optional[str]) -> bool:
    """判斷是否能使用公告牆"""
    if role == "admin":
        return True
    try:
        perms = json.loads(permissions) if permissions else {}
        return bool(perms.get("approvals") or perms.get("memos"))
    except Exception:
        return False

def can_view_memo(visibility: str, user: str, role: Optional[str], author: str, permissions: Optional[str]) -> bool:
    """restricted 僅限作者本人可看，org 全公司可見"""
    if role == "admin":
        return True
    if (visibility or "org").lower() == "org":
        return can_use_portal(role, permissions)
    return (user and author and user == author)

# ========= 共用查詢 =========
def query_memos(
    q: str,
    author: str,
    visibility: str,   # all | org | restricted
    page: int,
    page_size: int,
    user: str,
    role: Optional[str],
) -> Dict[str, Any]:
    ensure_memos_table()
    cond, params = [], []

    # 可見性限制
    if role != "admin":
        base_visible = "(visibility='org' OR author=?)"
        params.append(user)
    else:
        base_visible = "1=1"

    if q:
        like = f"%{q}%"
        cond.append("(title LIKE ? COLLATE NOCASE OR body LIKE ? COLLATE NOCASE OR author LIKE ? COLLATE NOCASE)")
        params.extend([like, like, like])

    if author:
        cond.append("(author = ?)")
        params.append(author)

    if visibility in ("org", "restricted"):
        cond.append("(visibility = ?)")
        params.append(visibility)

    where = "WHERE " + base_visible + (" AND " + " AND ".join(cond) if cond else "")
    offset = (page - 1) * page_size

    with get_conn() as conn:
        c = conn.cursor()
        total = c.execute(f"SELECT COUNT(*) FROM memos {where}", params).fetchone()[0]
        rows = c.execute(
            f"""
              SELECT id, title, COALESCE(body,''), visibility, created_at,
                     COALESCE(author,''), COALESCE(source,''), COALESCE(source_id,'')
              FROM memos
              {where}
              ORDER BY datetime(created_at) DESC, id DESC
              LIMIT ? OFFSET ?
            """,
            params + [page_size, offset],
        ).fetchall()

    items = [
        {
            "id": r[0],
            "title": r[1],
            "preview": (r[2] or "")[:160],
            "visibility": (r[3] or "org"),
            "created_at": r[4],
            "author": r[5],
            "source": r[6],
            "source_id": r[7],
        }
        for r in rows
    ]
    return {"total": total, "items": items, "page": page, "page_size": page_size}

# ========= 清單頁 =========
@router.get("", response_class=HTMLResponse)
async def list_memos(
    request: Request,
    q: str = Query("", description="關鍵字"),
    author: str = Query("", description="作者"),
    visibility: str = Query("all", regex="^(all|org|restricted)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=5, le=100),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_portal(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    data = query_memos(q, author, visibility, page, page_size, user, role)
    ctx = get_base_context(request, user, role, permissions)
    ctx.update({"q": q, "author": author, "visibility": visibility, **data})
    return templates.TemplateResponse("memos/list.html", ctx)

# ========= AJAX：分頁 / 篩選 =========
@router.get("/api/list")
async def api_list(
    request: Request,
    q: str = "",
    author: str = "",
    visibility: str = Query("all", regex="^(all|org|restricted)$"),
    page: int = 1,
    page_size: int = 20,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not can_use_portal(role, permissions):
        return JSONResponse({"error": "permission_denied"}, status_code=403)
    data = query_memos(q, author, visibility, page, page_size, user, role)
    return JSONResponse({"ok": True, **data})

# ========= 詳情頁 =========
@router.get("/{memo_id}", response_class=HTMLResponse)
async def memo_detail(
    memo_id: int,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    ensure_memos_table()

    with get_conn() as conn:
        c = conn.cursor()
        r = c.execute(
            """SELECT id, title, COALESCE(body,''), visibility, created_at,
                      COALESCE(author,''), COALESCE(source,''), COALESCE(source_id,'')
               FROM memos WHERE id=?""",
            (memo_id,),
        ).fetchone()
        if not r:
            return HTMLResponse("Memo not found", status_code=404)

    if not can_view_memo(r[3], user, role, r[5], permissions):
        return HTMLResponse("你沒有權限檢視此公告。", status_code=403)

    item = {
        "id": r[0],
        "title": r[1],
        "body": r[2],
        "visibility": (r[3] or "org"),
        "created_at": r[4],
        "author": r[5],
        "source": r[6],
        "source_id": r[7],
    }

    ctx = get_base_context(request, user, role, permissions)
    ctx["item"] = item
    return templates.TemplateResponse("memos/detail.html", ctx)
