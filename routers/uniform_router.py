# file: routers/uniform_router.py
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import sqlite3, os, math
from typing import List, Optional
from app_utils import (
    templates,
    get_base_context,
    get_conn,
    ensure_permission_pack,
)  # 移除 user_requires

DB_PATH = os.getenv("UNIFORM_DB", "data/hr_uniform.db")

router = APIRouter()


# --- 安全相容的 get_base_context 包裝 ---
def safe_get_base_context(request):
    """
    相容兩種簽名：
    1) get_base_context(request)
    2) get_base_context(request, user, role, permissions_raw)
    盡量從 request.state 取 user/role/perms；若缺，給容錯預設。
    """
    try:
        # 先試舊版（單參數）
        return get_base_context(request)
    except TypeError:
        # 新版需要 user/role/permissions_raw
        user = getattr(request.state, "user", None)
        role = getattr(request.state, "role", None)
        perms = getattr(request.state, "permissions_raw", None)
        # 兼容某些站點把 perms 放在 request.state.permissions
        if perms is None:
            perms = getattr(request.state, "permissions", [])
        # 最後兜一個空清單，避免 None
        if perms is None:
            perms = []
        return get_base_context(request, user, role, perms)


# 權限檢查小幫手（在本檔案自帶，不依賴 app_utils）
def require_perm(perm_key: str):
    async def _check(request: Request):
        ctx = safe_get_base_context(request)

        # 盡量兼容你站上的權限欄位
        perms = set()
        for k in ("permissions_raw", "permissions", "perm_keys"):
            v = ctx.get(k)
            if isinstance(v, (list, set, tuple)):
                perms.update(v)

        # 管理者放行（同你站上的慣例旗標）
        if ctx.get("is_admin") or ctx.get("can_admin") or ("admin_sessions" in perms):
            return True

        if perm_key in perms:
            return True

        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Forbidden")

    return Depends(_check)


# --- DB bootstrap -------------------------------------------------
def ensure_tables():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.executescript(
        """
    CREATE TABLE IF NOT EXISTS uniform_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_no TEXT UNIQUE,
        to_dept TEXT DEFAULT '房務部',
        employee_name TEXT,
        gender TEXT,
        job_title TEXT,
        department TEXT,
        is_fulltime TEXT,
        intern_cycle TEXT,
        emp_no TEXT,
        onboard_date TEXT,
        transfer_date TEXT,
        unit_name TEXT,
        temp_cycle TEXT,
        school_name TEXT,
        hr_manager_sign TEXT,
        hr_sign_date TEXT,
        employee_idno TEXT,
        employee_sign TEXT,
        employee_sign_dt TEXT,
        note_carbon_copy TEXT,
        status TEXT DEFAULT 'draft',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE TABLE IF NOT EXISTS uniform_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL REFERENCES uniform_requests(id) ON DELETE CASCADE,
        use_date TEXT,
        item_name TEXT,
        qty INTEGER,
        remark TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_uniform_items_request ON uniform_items(request_id);
    """
    )
    conn.commit()
    conn.close()


# 自動補齊權限鍵
def ensure_permissions():
    ensure_permission_pack("uniform")  # 產生 uniform_view / uniform_manage


# 匯入時即確保
ensure_tables()
ensure_permissions()


# --- Utilities ----------------------------------------------------
def _conn():
    return sqlite3.connect(DB_PATH)


def _next_request_no(cur) -> str:
    # URYYYYMMDD-XXX
    cur.execute("SELECT strftime('%Y%m%d','now','localtime')")
    ymd = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(1) FROM uniform_requests WHERE created_at LIKE date('now','localtime')||'%%'"
    )
    n = cur.fetchone()[0] + 1
    return f"UR{ymd}-{n:03d}"


# --- Views --------------------------------------------------------
@router.get(
    "/uniform", response_class=HTMLResponse, dependencies=[require_perm("uniform_view")]
)
async def page_list(request: Request, page: int = 1, q: str = ""):
    ctx = get_base_context(request)
    ctx.update(
        {
            "active_menu": "uniform",
            "can_manage": ctx.get("can_admin")
            or ctx.get("can_permissions")
            or ctx.get("can_manage")
            or False,
        }
    )

    per = 20
    off = (page - 1) * per
    params = []
    where = ""
    if q:
        where = "WHERE employee_name LIKE ? OR emp_no LIKE ? OR request_no LIKE ?"
        params = [f"%{q}%", f"%{q}%", f"%{q}%"]

    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(1) FROM uniform_requests {where}", params)
    total = cur.fetchone()[0]
    pages = max(1, math.ceil(total / per))

    cur.execute(
        f"""
      SELECT id, request_no, employee_name, department, job_title, status, created_at
      FROM uniform_requests
      {where}
      ORDER BY id DESC
      LIMIT ? OFFSET ?
    """,
        params + [per, off],
    )
    rows = cur.fetchall()
    conn.close()

    ctx.update(
        {
            "rows": rows,
            "page": page,
            "pages": pages,
            "q": q,
        }
    )
    return templates.TemplateResponse("uniform/uniform_list.html", ctx)


@router.get(
    "/uniform/new",
    response_class=HTMLResponse,
    dependencies=[require_perm("uniform_manage")],
)
async def page_new(request: Request):
    ctx = get_base_context(request)
    # 取出權限鍵
    perms = set()
    for k in ("permissions", "permissions_raw", "perm_keys"):
        v = ctx.get(k)
        if isinstance(v, (list, set, tuple)):
            perms.update(v)

    can_manage = (
        ("uniform_manage" in perms) or ctx.get("is_admin") or ctx.get("can_admin")
    )
    ctx.update({"active_menu": "uniform", "can_manage": bool(can_manage)})


@router.get(
    "/uniform/edit/{rid}",
    response_class=HTMLResponse,
    dependencies=[require_perm("uniform_manage")],
)
async def page_edit(request: Request, rid: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM uniform_requests WHERE id=?", (rid,))
    head = cur.fetchone()
    cur.execute(
        "SELECT id, use_date, item_name, qty, remark FROM uniform_items WHERE request_id=? ORDER BY id",
        (rid,),
    )
    items = cur.fetchall()
    conn.close()

    ctx = get_base_context(request)
    ctx.update({"active_menu": "uniform", "mode": "edit", "head": head, "items": items})

    ctx.update({"active_menu": "uniform", "can_manage": True})

    return templates.TemplateResponse("uniform/uniform_form.html", ctx)


@router.post("/uniform/save", dependencies=[require_perm("uniform_manage")])
async def api_save(
    request: Request,
    rid: Optional[int] = Form(None),
    employee_name: str = Form(""),
    gender: str = Form(""),
    job_title: str = Form(""),
    department: str = Form(""),
    is_fulltime: str = Form(""),
    intern_cycle: str = Form(""),
    emp_no: str = Form(""),
    onboard_date: str = Form(""),
    transfer_date: str = Form(""),
    unit_name: str = Form(""),
    temp_cycle: str = Form(""),
    school_name: str = Form(""),
    hr_manager_sign: str = Form(""),
    hr_sign_date: str = Form(""),
    employee_idno: str = Form(""),
    employee_sign: str = Form(""),
    employee_sign_dt: str = Form(""),
    note_carbon_copy: str = Form(""),
    status: str = Form("draft"),
    # 明細：以多列輸入 name="item_use_date[]" 等
    item_use_date: List[str] = Form([]),
    item_name: List[str] = Form([]),
    item_qty: List[str] = Form([]),
    item_remark: List[str] = Form([]),
):
    conn = _conn()
    cur = conn.cursor()

    if rid:
        cur.execute(
            """
            UPDATE uniform_requests SET
              employee_name=?, gender=?, job_title=?, department=?, is_fulltime=?, intern_cycle=?,
              emp_no=?, onboard_date=?, transfer_date=?, unit_name=?, temp_cycle=?, school_name=?,
              hr_manager_sign=?, hr_sign_date=?, employee_idno=?, employee_sign=?, employee_sign_dt=?,
              note_carbon_copy=?, status=?, updated_at=datetime('now','localtime')
            WHERE id=?
        """,
            (
                employee_name,
                gender,
                job_title,
                department,
                is_fulltime,
                intern_cycle,
                emp_no,
                onboard_date,
                transfer_date,
                unit_name,
                temp_cycle,
                school_name,
                hr_manager_sign,
                hr_sign_date,
                employee_idno,
                employee_sign,
                employee_sign_dt,
                note_carbon_copy,
                status,
                rid,
            ),
        )
        cur.execute("DELETE FROM uniform_items WHERE request_id=?", (rid,))
        request_id = rid
    else:
        req_no = _next_request_no(cur)
        cur.execute(
            """
            INSERT INTO uniform_requests(
                request_no, employee_name, gender, job_title, department, is_fulltime, intern_cycle,
                emp_no, onboard_date, transfer_date, unit_name, temp_cycle, school_name,
                hr_manager_sign, hr_sign_date, employee_idno, employee_sign, employee_sign_dt,
                note_carbon_copy, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                req_no,
                employee_name,
                gender,
                job_title,
                department,
                is_fulltime,
                intern_cycle,
                emp_no,
                onboard_date,
                transfer_date,
                unit_name,
                temp_cycle,
                school_name,
                hr_manager_sign,
                hr_sign_date,
                employee_idno,
                employee_sign,
                employee_sign_dt,
                note_carbon_copy,
                status,
            ),
        )
        request_id = cur.lastrowid

    # 明細存檔
    for i in range(len(item_name)):
        n = (item_name[i] or "").strip()
        if not n:
            continue
        cur.execute(
            """
            INSERT INTO uniform_items(request_id, use_date, item_name, qty, remark)
            VALUES (?,?,?,?,?)
        """,
            (
                request_id,
                (item_use_date[i] if i < len(item_use_date) else ""),
                n,
                (
                    int(item_qty[i])
                    if i < len(item_qty) and item_qty[i].isdigit()
                    else None
                ),
                (item_remark[i] if i < len(item_remark) else ""),
            ),
        )
    conn.commit()
    conn.close()
    return RedirectResponse(url="/uniform", status_code=303)


@router.post("/uniform/delete/{rid}", dependencies=[require_perm("uniform_manage")])
async def api_delete(request: Request, rid: int):

    conn = _conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM uniform_requests WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})
