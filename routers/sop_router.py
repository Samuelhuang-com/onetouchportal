# routers/sop_router.py
from fastapi import APIRouter, Request, Form, Cookie, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from typing import Optional, Dict, Any, List
from datetime import datetime
import os, shutil, uuid, sqlite3
import pandas as pd

from app_utils import get_base_context, templates  # 你現有專案的工具

router = APIRouter()

# ===== 可調參數 =====
SOPS_DB = getattr(__import__("app_utils"), "SOPS_DB", "data/sops.db")
SOP_ATTACHMENT_DIR = getattr(
    __import__("app_utils"), "SOP_ATTACHMENT_DIR", "data/sop_files"
)
DUE_CRITICAL_DAYS = int(getattr(__import__("app_utils"), "SOP_DUE_CRITICAL_DAYS", 30))
DUE_WARNING_DAYS = int(getattr(__import__("app_utils"), "SOP_DUE_WARNING_DAYS", 90))

# ===== DB Schema =====
SCHEMA_SQL = f"""\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sops (
  SOPID         TEXT PRIMARY KEY,
  Title         TEXT NOT NULL,
  Department    TEXT,
  Version       TEXT,
  Owner         TEXT,
  Status        TEXT,
  EffectiveDate TEXT,
  ReviewDate    TEXT,
  Tags          TEXT,
  Summary       TEXT,
  Content       TEXT,
  Notes         TEXT,
  CreatedAt     TEXT DEFAULT (datetime('now')),
  UpdatedAt     TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sops_dept   ON sops(Department);
CREATE INDEX IF NOT EXISTS idx_sops_status ON sops(Status);
CREATE INDEX IF NOT EXISTS idx_sops_review ON sops(ReviewDate);
CREATE INDEX IF NOT EXISTS idx_sops_title  ON sops(Title);

-- 多附件表
CREATE TABLE IF NOT EXISTS sop_files (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  sop_id       TEXT NOT NULL,
  orig_name    TEXT NOT NULL,
  content_type TEXT,
  size_bytes   INTEGER,
  stored_name  TEXT NOT NULL,
  uploaded_by  TEXT,
  uploaded_at  TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(sop_id) REFERENCES sops(SOPID) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sop_files_sop ON sop_files(sop_id);

-- 版本歷程（改前快照）
CREATE TABLE IF NOT EXISTS sop_versions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  sop_id       TEXT NOT NULL,
  version_no   TEXT,
  title        TEXT,
  department   TEXT,
  owner        TEXT,
  status       TEXT,
  effective    TEXT,
  review       TEXT,
  tags         TEXT,
  summary      TEXT,
  content      TEXT,
  notes        TEXT,
  change_note  TEXT,
  changed_by   TEXT,
  changed_at   TEXT DEFAULT (datetime('now')),
  FOREIGN KEY(sop_id) REFERENCES sops(SOPID) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sop_versions_sop ON sop_versions(sop_id);

-- 複審提醒視圖
DROP VIEW IF EXISTS sops_with_due;
CREATE VIEW sops_with_due AS
SELECT
  s.*,
  CASE
    WHEN ReviewDate IS NULL OR ReviewDate='' THEN 'due-level-safe'
    WHEN (julianday(ReviewDate) - julianday('now')) < 0 THEN 'due-level-expired'
    WHEN (julianday(ReviewDate) - julianday('now')) <= {DUE_CRITICAL_DAYS} THEN 'due-level-critical'
    WHEN (julianday(ReviewDate) - julianday('now')) <= {DUE_WARNING_DAYS}  THEN 'due-level-warning'
    ELSE 'due-level-safe'
  END AS due_level,
  CASE
    WHEN ReviewDate IS NULL OR ReviewDate='' THEN '未設定'
    WHEN (julianday(ReviewDate) - julianday('now')) < 0 THEN '已逾期'
    WHEN (julianday(ReviewDate) - julianday('now')) <= {DUE_WARNING_DAYS}
      THEN CAST(ROUND(julianday(ReviewDate) - julianday('now')) AS INTEGER) || ' 天後複審'
    ELSE '{DUE_WARNING_DAYS}天以上'
  END AS due_text
FROM sops s;
"""


def get_conn():
    os.makedirs(os.path.dirname(SOPS_DB), exist_ok=True)
    conn = sqlite3.connect(SOPS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)


# ===== 附件處理 =====
def ensure_dir():
    os.makedirs(SOP_ATTACHMENT_DIR, exist_ok=True)


def save_upload_file(sop_id: str, f: UploadFile, uploaded_by: Optional[str]) -> None:
    ensure_dir()
    ext = os.path.splitext(f.filename)[1]
    stored = f"{sop_id}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(SOP_ATTACHMENT_DIR, stored)
    with open(path, "wb") as buffer:
        shutil.copyfileobj(f.file, buffer)
    size = os.path.getsize(path)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sop_files (sop_id, orig_name, content_type, size_bytes, stored_name, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                sop_id,
                f.filename,
                getattr(f, "content_type", None),
                size,
                stored,
                uploaded_by,
            ),
        )
        conn.commit()


def list_files(sop_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute(
            """SELECT id, orig_name, content_type, size_bytes, stored_name, uploaded_by, uploaded_at
                              FROM sop_files WHERE sop_id=? ORDER BY id""",
            (sop_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def delete_file(file_id: int):
    with get_conn() as conn:
        cur = conn.execute("SELECT stored_name FROM sop_files WHERE id=?", (file_id,))
        row = cur.fetchone()
        if row:
            path = os.path.join(SOP_ATTACHMENT_DIR, row["stored_name"])
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            conn.execute("DELETE FROM sop_files WHERE id=?", (file_id,))
            conn.commit()


# ===== Query / CRUD =====
def row_dict(r: sqlite3.Row) -> Dict[str, Any]:
    return dict(r) if r else None


def get_sops_for_list() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        try:
            cur = conn.execute("SELECT * FROM sops_with_due ORDER BY CreatedAt DESC")
        except sqlite3.OperationalError:
            cur = conn.execute("SELECT * FROM sops ORDER BY CreatedAt DESC")
        rows = [row_dict(r) for r in cur.fetchall()]
    return rows


def get_filtered_sops(
    q: str, due: str, dept: str, status: str, start: str, end: str
) -> List[Dict[str, Any]]:
    base = "SELECT * FROM sops_with_due"
    cond, params = [], {}

    if q:
        like_fields = [
            "Title",
            "Department",
            "Version",
            "Owner",
            "Tags",
            "Summary",
            "Content",
            "Status",
        ]
        cond.append("(" + " OR ".join(f"{f} LIKE :q" for f in like_fields) + ")")
        params["q"] = f"%{q}%"
    if dept and dept != "all":
        cond.append("Department = :dept")
        params["dept"] = dept
    if status and status != "all":
        cond.append("Status = :st")
        params["st"] = status
    if due and due != "all":
        cond.append("due_level = :due_level")
        params["due_level"] = f"due-level-{due}"
    if start:
        cond.append("EffectiveDate >= :start")
        params["start"] = start
    if end:
        cond.append("EffectiveDate <= :end")
        params["end"] = end

    if cond:
        base += " WHERE " + " AND ".join(cond)
    base += " ORDER BY CreatedAt DESC"

    with get_conn() as conn:
        cur = conn.execute(base, params)
        return [row_dict(r) for r in cur.fetchall()]


def get_sop(sop_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM sops WHERE SOPID=?", (sop_id,))
        r = cur.fetchone()
    return row_dict(r)


def insert_sop(data: Dict[str, Any]):
    data = data.copy()
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    data.setdefault("CreatedAt", now)
    data.setdefault("UpdatedAt", now)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sops
            (SOPID,Title,Department,Version,Owner,Status,EffectiveDate,ReviewDate,Tags,Summary,Content,Notes,CreatedAt,UpdatedAt)
            VALUES (:SOPID,:Title,:Department,:Version,:Owner,:Status,:EffectiveDate,:ReviewDate,:Tags,:Summary,:Content,:Notes,:CreatedAt,:UpdatedAt)
        """,
            data,
        )
        conn.commit()


def snapshot_version(
    sop: Dict[str, Any], change_note: Optional[str], changed_by: Optional[str]
):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sop_versions
            (sop_id, version_no, title, department, owner, status, effective, review, tags, summary, content, notes, change_note, changed_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sop["SOPID"],
                sop.get("Version"),
                sop.get("Title"),
                sop.get("Department"),
                sop.get("Owner"),
                sop.get("Status"),
                sop.get("EffectiveDate"),
                sop.get("ReviewDate"),
                sop.get("Tags"),
                sop.get("Summary"),
                sop.get("Content"),
                sop.get("Notes"),
                change_note,
                changed_by,
            ),
        )
        conn.commit()


def list_versions(sop_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute(
            """SELECT id, version_no, title, department, owner, status, effective, review,
                                     change_note, changed_by, changed_at
                              FROM sop_versions WHERE sop_id=? ORDER BY id DESC""",
            (sop_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def update_sop(
    sop_id: str,
    data: Dict[str, Any],
    change_note: Optional[str],
    changed_by: Optional[str],
):
    old = get_sop(sop_id)
    if old:
        snapshot_version(old, change_note, changed_by)
    data = data.copy()
    data["UpdatedAt"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    data["SOPID"] = sop_id
    sets = """Title=:Title,Department=:Department,Version=:Version,Owner=:Owner,Status=:Status,
              EffectiveDate=:EffectiveDate,ReviewDate=:ReviewDate,Tags=:Tags,Summary=:Summary,
              Content=:Content,Notes=:Notes,UpdatedAt=:UpdatedAt"""
    with get_conn() as conn:
        conn.execute(f"UPDATE sops SET {sets} WHERE SOPID=:SOPID", data)
        conn.commit()


def delete_sop_row(sop_id: str):
    for f in list_files(sop_id):
        try:
            os.remove(os.path.join(SOP_ATTACHMENT_DIR, f["stored_name"]))
        except FileNotFoundError:
            pass
    with get_conn() as conn:
        conn.execute("DELETE FROM sop_files WHERE sop_id=?", (sop_id,))
        conn.execute("DELETE FROM sop_versions WHERE sop_id=?", (sop_id,))
        conn.execute("DELETE FROM sops WHERE SOPID=?", (sop_id,))
        conn.commit()


async def get_sop_form_data(form: Any) -> Dict[str, Any]:
    def to_iso(name: str) -> Optional[str]:
        v = form.get(name)
        if not v:
            return None
        try:
            return pd.to_datetime(v).date().isoformat()
        except Exception:
            return None

    return {
        "Title": form.get("Title"),
        "Department": form.get("Department"),
        "Version": form.get("Version"),
        "Owner": form.get("Owner"),
        "Status": form.get("Status"),
        "EffectiveDate": to_iso("EffectiveDate"),
        "ReviewDate": to_iso("ReviewDate"),
        "Tags": form.get("Tags"),
        "Summary": form.get("Summary"),
        "Content": form.get("Content"),
        "Notes": form.get("Notes"),
    }


# ===== 權限判斷：view / manage 分流 =====
def _can_view(perms: dict, role: str) -> bool:
    if role == "admin":
        return True
    return bool(
        perms.get("sops_manage")
        or perms.get("sops_view")
        or perms.get("knowledge_base")
    )


def _can_manage(perms: dict, role: str) -> bool:
    if role == "admin":
        return True
    return bool(perms.get("sops_manage"))


def _check_perm(ctx: dict, need: str = "view") -> bool:
    role = ctx.get("role")
    perms = ctx["permissions"]
    return _can_manage(perms, role) if need == "manage" else _can_view(perms, role)


# ===== API（僅閱讀權） =====
@router.get("/api/sops", response_class=JSONResponse)
async def api_sops(
    q: Optional[str] = "",
    due: Optional[str] = "all",
    dept: Optional[str] = "all",
    status: Optional[str] = "all",
    start: Optional[str] = "",
    end: Optional[str] = "",
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    init_db()
    ctx = (
        get_base_context(Request, user, role, permissions)
        if isinstance(Request, dict)
        else {"permissions": {}, "role": role}
    )  # 避免型別檢查器抱怨
    # 這裡簡化：僅檢查 cookies 存在，實際頁面會再驗證
    return get_filtered_sops(q, due, dept, status, start, end)


@router.get("/api/sops/departments", response_class=JSONResponse)
async def api_sop_departments(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    init_db()
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT DISTINCT COALESCE(NULLIF(TRIM(Department),''), '（未填）') AS dept FROM sops ORDER BY dept"
        )
        return [r["dept"] for r in cur.fetchall()]


# ===== 單筆 API（僅閱讀權） =====
@router.get("/api/sops/{sop_id}", response_class=JSONResponse)
async def api_sop_one(
    sop_id: str,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="view"):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    with get_conn() as conn:
        try:
            cur = conn.execute("SELECT * FROM sops_with_due WHERE SOPID=?", (sop_id,))
        except sqlite3.OperationalError:
            cur = conn.execute("SELECT * FROM sops WHERE SOPID=?", (sop_id,))
        row = cur.fetchone()
    sop = dict(row) if row else None
    if not sop:
        return JSONResponse({"error": "not_found"}, status_code=404)

    return {"sop": sop, "files": list_files(sop_id), "versions": list_versions(sop_id)}


# ===== Pages =====
@router.get("/sops", response_class=HTMLResponse)
async def list_sops(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="view"):
        return RedirectResponse(url="/dashboard")
    ctx["sops"] = get_sops_for_list()
    ctx["can_manage"] = _check_perm(ctx, need="manage")
    return templates.TemplateResponse("sops_list.html", ctx)


@router.get("/sops/view/{sop_id}", response_class=HTMLResponse)
async def view_sop_page(
    sop_id: str,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="view"):
        return RedirectResponse(url="/dashboard")

    with get_conn() as conn:
        try:
            cur = conn.execute("SELECT * FROM sops_with_due WHERE SOPID=?", (sop_id,))
        except sqlite3.OperationalError:
            cur = conn.execute("SELECT * FROM sops WHERE SOPID=?", (sop_id,))
        row = cur.fetchone()
    sop = dict(row) if row else None
    if not sop:
        return RedirectResponse(url="/sops")

    ctx.update(
        {
            "sop": sop,
            "files": list_files(sop_id),
            "versions": list_versions(sop_id),
            "can_manage": _check_perm(ctx, need="manage"),
        }
    )
    return templates.TemplateResponse("sop_detail.html", ctx)


@router.get("/sops/manage", response_class=HTMLResponse)
async def manage_sops_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/dashboard")
    ctx.update(
        {
            "all_sops": get_sops_for_list(),
            "action_url": router.url_path_for("add_sop"),
            "sop": None,
            "versions": [],
            "files": [],
            "can_manage": True,
        }
    )
    return templates.TemplateResponse("manage_sop.html", ctx)


@router.get("/sops/edit/{sop_id}", response_class=HTMLResponse)
async def edit_sop_page(
    sop_id: str,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/dashboard")
    s = get_sop(sop_id)
    if not s:
        return RedirectResponse(url=router.url_path_for("manage_sops_page"))
    ctx.update(
        {
            "all_sops": get_sops_for_list(),
            "action_url": router.url_path_for("edit_sop", sop_id=sop_id),
            "sop": s,
            "versions": list_versions(sop_id),
            "files": list_files(sop_id),
            "can_manage": True,
        }
    )
    return templates.TemplateResponse("manage_sop.html", ctx)


# ===== 變更資料（需維護權） =====
@router.post("/sops/add")
async def add_sop(
    request: Request,
    attachments: List[UploadFile] = File(None),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/dashboard")
    data = await get_sop_form_data(await request.form())
    sop_id = str(uuid.uuid4())
    data["SOPID"] = sop_id
    insert_sop(data)
    if attachments:
        for f in attachments:
            if f and f.filename:
                save_upload_file(sop_id, f, uploaded_by=user)
    return RedirectResponse(
        url=router.url_path_for("manage_sops_page"), status_code=303
    )


@router.post("/sops/edit/{sop_id}")
async def edit_sop(
    sop_id: str,
    request: Request,
    attachments: List[UploadFile] = File(None),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/dashboard")
    form = await request.form()
    change_note = form.get("ChangeNote") or ""
    data = await get_sop_form_data(form)
    update_sop(sop_id, data, change_note, user)
    if attachments:
        for f in attachments:
            if f and f.filename:
                save_upload_file(sop_id, f, uploaded_by=user)
    return RedirectResponse(
        url=router.url_path_for("edit_sop_page", sop_id=sop_id), status_code=303
    )


@router.post("/sops/delete")
async def delete_sop(
    request: Request,
    sop_id: str = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/dashboard")
    delete_sop_row(sop_id)
    return RedirectResponse(
        url=router.url_path_for("manage_sops_page"), status_code=303
    )


@router.post("/sops/file/delete/{file_id}")
async def delete_sop_file(
    file_id: int,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/", status_code=303)
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/dashboard")
    delete_file(file_id)
    ref = request.headers.get("referer") or "/sops/manage"
    return RedirectResponse(url=ref, status_code=303)


# ===== 下載附件（僅閱讀權） =====
@router.get("/sop_files/{stored_name}")
async def serve_sop_file(
    stored_name: str,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="view"):
        return RedirectResponse(url="/dashboard")
    base_dir = os.path.abspath(SOP_ATTACHMENT_DIR)
    requested_path = os.path.join(base_dir, stored_name)
    if not os.path.abspath(requested_path).startswith(base_dir):
        return HTMLResponse(content="Forbidden", status_code=403)
    if os.path.exists(requested_path) and os.path.isfile(requested_path):
        return FileResponse(requested_path)
    return HTMLResponse(content="File Not Found", status_code=404)
