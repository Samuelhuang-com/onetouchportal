# routers/contract_router.py
from fastapi import APIRouter, Request, Form, Cookie, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from typing import Optional, Dict, Any, List
from datetime import datetime
import os, shutil, uuid, sqlite3, json
import pandas as pd

from app_utils import get_base_context, templates, CONTRACTS_FILE, CONTRACT_ATTACHMENT_DIR

router = APIRouter(tags=["Contracts"])

# ---------- DB ----------
CONTRACTS_DB = getattr(__import__("app_utils"), "CONTRACTS_DB", "data/contracts.db")

SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS contracts (
  ContractID     TEXT PRIMARY KEY,
  VendorName     TEXT NOT NULL,
  ContractType   TEXT,
  Subject        TEXT,
  Content        TEXT,
  Amount         REAL DEFAULT 0,
  StartDate      TEXT,
  EndDate        TEXT,
  ContactPerson  TEXT,
  ContactPhone   TEXT,
  Status         TEXT,
  Notes          TEXT,
  CreatedAt      TEXT DEFAULT (datetime('now')),
  UpdatedAt      TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contracts_vendor    ON contracts(VendorName);
CREATE INDEX IF NOT EXISTS idx_contracts_status    ON contracts(Status);
CREATE INDEX IF NOT EXISTS idx_contracts_enddate   ON contracts(EndDate);
CREATE INDEX IF NOT EXISTS idx_contracts_startdate ON contracts(StartDate);

-- 多附件
CREATE TABLE IF NOT EXISTS contract_files (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id  TEXT NOT NULL,
  stored_name  TEXT NOT NULL,
  orig_name    TEXT,
  mime_type    TEXT,
  size         INTEGER,
  uploaded_at  TEXT DEFAULT (datetime('now')),
  uploaded_by  TEXT,
  FOREIGN KEY (contract_id) REFERENCES contracts(ContractID) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_files_contract ON contract_files(contract_id);

-- 版本歷程（存整筆 snapshot）
CREATE TABLE IF NOT EXISTS contract_versions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id  TEXT NOT NULL,
  version_no   INTEGER NOT NULL,
  snapshot_json TEXT NOT NULL,
  created_at   TEXT DEFAULT (datetime('now')),
  created_by   TEXT,
  change_note  TEXT,
  FOREIGN KEY (contract_id) REFERENCES contracts(ContractID) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_unique ON contract_versions(contract_id, version_no);

DROP VIEW IF EXISTS contracts_with_expiry;
CREATE VIEW contracts_with_expiry AS
SELECT
  c.*,
  CASE
    WHEN EndDate IS NULL OR EndDate='' THEN 'expiry-level-safe'
    WHEN (julianday(EndDate) - julianday('now')) < 0 THEN 'expiry-level-expired'
    WHEN (julianday(EndDate) - julianday('now')) <= 30 THEN 'expiry-level-critical'
    WHEN (julianday(EndDate) - julianday('now')) <= 90 THEN 'expiry-level-warning'
    ELSE 'expiry-level-safe'
  END AS expiry_level,
  CASE
    WHEN EndDate IS NULL OR EndDate='' THEN '未設定'
    WHEN (julianday(EndDate) - julianday('now')) < 0 THEN '已到期'
    WHEN (julianday(EndDate) - julianday('now')) <= 90
      THEN CAST(ROUND(julianday(EndDate) - julianday('now')) AS INTEGER) || ' 天後到期'
    ELSE '90天以上'
  END AS expiry_text
FROM contracts c;
"""

def get_conn():
    os.makedirs(os.path.dirname(CONTRACTS_DB), exist_ok=True)
    conn = sqlite3.connect(CONTRACTS_DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)

# ---------- 附件 ----------
def _ensure_dir():
    os.makedirs(CONTRACT_ATTACHMENT_DIR, exist_ok=True)

def list_files(contract_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM contract_files WHERE contract_id=? ORDER BY uploaded_at DESC", (contract_id,))
        return [dict(r) for r in cur.fetchall()]

def get_file(file_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM contract_files WHERE id=?", (file_id,)).fetchone()
        return dict(r) if r else None

def add_files(contract_id: str, files: List[UploadFile], uploaded_by: str):
    if not files: return
    _ensure_dir()
    rows = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1]
        stored = f"{contract_id}_{uuid.uuid4().hex}{ext}"
        path = os.path.join(CONTRACT_ATTACHMENT_DIR, stored)
        with open(path, "wb") as buf:
            shutil.copyfileobj(f.file, buf)
        size = os.path.getsize(path)
        rows.append({
            "contract_id": contract_id,
            "stored_name": stored,
            "orig_name": f.filename,
            "mime_type": f.content_type or "",
            "size": size,
            "uploaded_by": uploaded_by or "",
        })
    if rows:
        with get_conn() as conn:
            conn.executemany(
                """INSERT INTO contract_files (contract_id,stored_name,orig_name,mime_type,size,uploaded_by)
                   VALUES (:contract_id,:stored_name,:orig_name,:mime_type,:size,:uploaded_by)""",
                rows
            ); conn.commit()

def delete_file(file_id: int):
    row = get_file(file_id)
    if not row: return
    try:
        os.remove(os.path.join(CONTRACT_ATTACHMENT_DIR, row["stored_name"]))
    except FileNotFoundError:
        pass
    with get_conn() as conn:
        conn.execute("DELETE FROM contract_files WHERE id=?", (file_id,)); conn.commit()

# ---------- 版本 ----------
def _row_to_contract_dict(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    for k in ("StartDate","EndDate"):
        v = d.get(k)
        try:
            d[k] = datetime.fromisoformat(v) if v else None
        except Exception:
            d[k] = None
    return d

def snapshot_current(contract_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM contracts WHERE ContractID=?", (contract_id,)).fetchone()
    return dict(r) if r else None

def add_version(contract_id: str, created_by: str, note: str):
    current = snapshot_current(contract_id)
    if not current: return
    snap = json.dumps(current, ensure_ascii=False)
    with get_conn() as conn:
        cur = conn.execute("SELECT COALESCE(MAX(version_no), 0) AS v FROM contract_versions WHERE contract_id=?", (contract_id,))
        next_v = (cur.fetchone()["v"] or 0) + 1
        conn.execute(
            """INSERT INTO contract_versions (contract_id,version_no,snapshot_json,created_by,change_note)
               VALUES (?,?,?,?,?)""",
            (contract_id, next_v, snap, created_by or "", note)
        ); conn.commit()

def list_versions(contract_id: str) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id, version_no, created_at, created_by, change_note FROM contract_versions WHERE contract_id=? ORDER BY version_no DESC",
            (contract_id,)
        )
        return [dict(r) for r in cur.fetchall()]

def get_version(version_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM contract_versions WHERE id=?", (version_id,)).fetchone()
        if not r: return None
        d = dict(r)
        try:
            d["snapshot"] = json.loads(d["snapshot_json"])
        except Exception:
            d["snapshot"] = {}
        return d

# ---------- Excel → SQLite（支援中英欄位） ----------
def _normalize_excel_columns(df: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "合約編號":"ContractID","Contract ID":"ContractID",
        "廠商名稱":"VendorName","供應商":"VendorName","商家名稱":"VendorName",
        "合約類型":"ContractType",
        "合約主旨":"Subject","主旨":"Subject","標題":"Subject",
        "合約內容":"Content","內容":"Content",
        "合約金額":"Amount","金額":"Amount",
        "開始日期":"StartDate","起始日":"StartDate",
        "結束日期":"EndDate","到期日":"EndDate",
        "廠商聯絡人":"ContactPerson","聯絡人":"ContactPerson",
        "聯絡人電話":"ContactPhone","電話":"ContactPhone",
        "狀態":"Status",
        "備註":"Notes",
    }
    for zh, en in mapping.items():
        if zh in df.columns and en not in df.columns:
            df[en] = df[zh]
    return df

def import_excel_to_sqlite():
    with get_conn() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        if cnt > 0: return
    if not (CONTRACTS_FILE and os.path.exists(CONTRACTS_FILE)): return

    df = pd.read_excel(CONTRACTS_FILE, engine="openpyxl")
    df = _normalize_excel_columns(df)

    expected = ["ContractID","VendorName","ContractType","Subject","Content","Amount",
                "StartDate","EndDate","ContactPerson","ContactPhone","Status","Notes"]
    for col in expected:
        if col not in df.columns: df[col] = None

    def to_iso(x):
        if pd.isna(x) or x is None or str(x).strip()=="":
            return None
        try: return pd.to_datetime(x).date().isoformat()
        except Exception: return None

    df["StartDate"] = df["StartDate"].apply(to_iso)
    df["EndDate"]   = df["EndDate"].apply(to_iso)
    df["Amount"]    = pd.to_numeric(df["Amount"], errors="coerce").fillna(0).astype(float)

    df["ContractID"] = df["ContractID"].astype(str).fillna("")
    mask = df["ContractID"].eq("") | df["ContractID"].eq("nan")
    df.loc[mask, "ContractID"] = [str(uuid.uuid4()) for _ in range(mask.sum())]

    rows = df[expected].to_dict("records")
    if not rows: return
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO contracts
               (ContractID,VendorName,ContractType,Subject,Content,Amount,StartDate,EndDate,ContactPerson,ContactPhone,Status,Notes,CreatedAt,UpdatedAt)
               VALUES (:ContractID,:VendorName,:ContractType,:Subject,:Content,:Amount,:StartDate,:EndDate,:ContactPerson,:ContactPhone,:Status,:Notes,datetime('now'),datetime('now'))""",
            rows
        ); conn.commit()

# ---------- Query / CRUD ----------
def _row_to_dict(r: sqlite3.Row) -> Dict[str, Any]:
    d = dict(r)
    for k in ("StartDate","EndDate"):
        v = d.get(k)
        try:
            d[k] = datetime.fromisoformat(v) if v else None
        except Exception:
            d[k] = None
    return d

def _row_to_api(r: sqlite3.Row) -> Dict[str, Any]:
    return dict(r)

def get_contracts_for_list() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        try:
            cur = conn.execute("SELECT * FROM contracts_with_expiry ORDER BY CreatedAt DESC")
        except sqlite3.OperationalError:
            cur = conn.execute("SELECT * FROM contracts ORDER BY CreatedAt DESC")
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    for d in rows:
        d["files"] = list_files(d["ContractID"])
    return rows

def get_filtered_contracts(q: str, expiry: str, start: str, end: str) -> List[Dict[str, Any]]:
    base, cond, p = "SELECT * FROM contracts_with_expiry", [], {}
    if q:
        like_fs = ["VendorName","ContractType","Subject","Content","ContactPerson","ContactPhone","Status"]
        cond.append("(" + " OR ".join(f"{f} LIKE :q" for f in like_fs) + ")")
        p["q"] = f"%{q}%"
    if expiry and expiry != "all":
        cond.append("expiry_level = :lvl"); p["lvl"] = f"expiry-level-{expiry}"
    if start:
        cond.append("EndDate >= :start"); p["start"] = start
    if end:
        cond.append("StartDate <= :end"); p["end"] = end
    if cond:
        base += " WHERE " + " AND ".join(cond)
    base += " ORDER BY CreatedAt DESC"
    with get_conn() as conn:
        rows = [ _row_to_api(r) for r in conn.execute(base, p).fetchall() ]
    for d in rows:
        d["files"] = list_files(d["ContractID"])
    return rows

def get_all_contracts() -> List[Dict[str, Any]]:
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM contracts ORDER BY EndDate DESC")
        rows = [_row_to_dict(r) for r in cur.fetchall()]
    for d in rows:
        d["files"] = list_files(d["ContractID"])
    return rows

def get_contract(contract_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        r = conn.execute("SELECT * FROM contracts WHERE ContractID=?", (contract_id,)).fetchone()
    return _row_to_dict(r) if r else None

def insert_contract(data: Dict[str, Any]):
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    data = data.copy(); data.update({"CreatedAt":now,"UpdatedAt":now})
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO contracts
               (ContractID,VendorName,ContractType,Subject,Content,Amount,StartDate,EndDate,ContactPerson,ContactPhone,Status,Notes,CreatedAt,UpdatedAt)
               VALUES (:ContractID,:VendorName,:ContractType,:Subject,:Content,:Amount,:StartDate,:EndDate,:ContactPerson,:ContactPhone,:Status,:Notes,:CreatedAt,:UpdatedAt)""",
            data
        ); conn.commit()

def update_contract(contract_id: str, data: Dict[str, Any]):
    data = data.copy(); data["UpdatedAt"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    data["ContractID"] = contract_id
    sets = ("VendorName=:VendorName,ContractType=:ContractType,Subject=:Subject,Content=:Content,"
            "Amount=:Amount,StartDate=:StartDate,EndDate=:EndDate,ContactPerson=:ContactPerson,"
            "ContactPhone=:ContactPhone,Status=:Status,Notes=:Notes,UpdatedAt=:UpdatedAt")
    with get_conn() as conn:
        conn.execute(f"UPDATE contracts SET {sets} WHERE ContractID=:ContractID", data); conn.commit()

def delete_contract_row(contract_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM contracts WHERE ContractID=?", (contract_id,)); conn.commit()

# ---------- 權限：view / manage ----------
def _can_view(perms: dict, role: Optional[str]) -> bool:
    if role == "admin": return True
    return bool(perms.get("contracts_manage") or perms.get("contracts_view") or perms.get("contracts"))

def _can_manage(perms: dict, role: Optional[str]) -> bool:
    if role == "admin": return True
    return bool(perms.get("contracts_manage") or perms.get("contracts_edit"))

def _check_perm(ctx: dict, need: str="view") -> bool:
    role = ctx.get("role")
    perms = ctx.get("permissions", {})
    return _can_manage(perms, role) if need=="manage" else _can_view(perms, role)

# ---------- 表單抽取 ----------
async def get_contract_form_data(form) -> Dict[str, Any]:
    def to_iso(name: str) -> Optional[str]:
        v = form.get(name)
        if not v: return None
        try: return pd.to_datetime(v).date().isoformat()
        except Exception: return None
    def to_float(v, default=0.0):
        try: return float(v)
        except Exception: return default

    return {
        "VendorName": form.get("VendorName"),
        "ContractType": form.get("ContractType"),
        "Subject": form.get("Subject"),
        "Content": form.get("Content"),
        "Amount": to_float(form.get("Amount", 0)),
        "Status": form.get("Status"),
        "StartDate": to_iso("StartDate"),
        "EndDate": to_iso("EndDate"),
        "ContactPerson": form.get("ContactPerson"),
        "ContactPhone": form.get("ContactPhone"),
        "Notes": form.get("Notes"),
    }

# ---------- API（需 view） ----------
@router.get("/api/contracts", response_class=JSONResponse)
async def api_contracts(
    request: Request,
    q: Optional[str] = "",
    expiry: Optional[str] = "all",
    start: Optional[str] = "",
    end: Optional[str] = "",
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return JSONResponse({"error":"unauthorized"}, status_code=401)
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="view"):
        return JSONResponse({"error":"forbidden"}, status_code=403)
    return get_filtered_contracts(q or "", expiry or "all", start or "", end or "")

# ---------- Pages ----------
@router.get("/contracts", response_class=HTMLResponse)
async def list_contracts(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    import_excel_to_sqlite()

    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="view"):
        return RedirectResponse(url="/dashboard")
    ctx["contracts"] = get_contracts_for_list()
    ctx["can_manage"] = _check_perm(ctx, need="manage")
    ctx["can_edit"] = ctx["can_manage"]  # 舊模板相容
    return templates.TemplateResponse("contract/contracts_list.html", ctx)

@router.get("/contracts/view/{contract_id}", response_class=HTMLResponse)
async def contract_detail_page(
    contract_id: str,
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

    c = get_contract(contract_id)
    if not c: return RedirectResponse(url="/contracts")

    c["files"] = list_files(contract_id)
    c["versions"] = list_versions(contract_id)
    ctx.update({"contract": c, "can_manage": _check_perm(ctx, need="manage")})
    return templates.TemplateResponse("contract/contract_detail.html", ctx)

@router.get("/contracts/version/{version_id}", response_class=HTMLResponse)
async def view_version_snapshot(
    version_id: int,
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
    v = get_version(version_id)
    if not v: return RedirectResponse(url="/contracts")
    ctx["version"] = v
    return templates.TemplateResponse("contract/contract_version.html", ctx)

@router.get("/contracts/manage", response_class=HTMLResponse)
async def manage_contracts_page(
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
        return RedirectResponse(url="/contracts")
    ctx.update({
        "all_contracts": get_all_contracts(),
        "action_url": router.url_path_for("add_contract"),
        "contract": None,
        "can_manage": True,
        "can_edit": True,
    })
    return templates.TemplateResponse("contract/manage_contract.html", ctx)

@router.get("/contracts/edit/{contract_id}", response_class=HTMLResponse)
async def edit_contract_page(
    contract_id: str,
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
        return RedirectResponse(url="/contracts")
    c = get_contract(contract_id)
    if not c: return RedirectResponse(url=router.url_path_for("manage_contracts_page"))
    c["files"] = list_files(contract_id)
    c["versions"] = list_versions(contract_id)
    ctx.update({
        "all_contracts": get_all_contracts(),
        "action_url": router.url_path_for("edit_contract", contract_id=contract_id),
        "contract": c,
        "can_manage": True,
        "can_edit": True,
    })
    return templates.TemplateResponse("contract/manage_contract.html", ctx)

# ---------- Mutations（需 manage） ----------
@router.post("/contracts/add")
async def add_contract(
    request: Request,
    files: List[UploadFile] = File(None),  # 多附件
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/contracts")

    form = await request.form()
    data = await get_contract_form_data(form)
    contract_id = str(uuid.uuid4())
    data["ContractID"] = contract_id

    insert_contract(data)
    add_version(contract_id, user, "初始建立")
    if files:
        add_files(contract_id, files, user)

    return RedirectResponse(url=router.url_path_for("manage_contracts_page"), status_code=303)

@router.post("/contracts/edit/{contract_id}")
async def edit_contract(
    contract_id: str,
    request: Request,
    files: List[UploadFile] = File(None),  # 多附件
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    change_note: Optional[str] = Form("編輯前快照"),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/contracts")

    # 先保存快照
    add_version(contract_id, user, change_note or "編輯前快照")

    form = await request.form()
    data = await get_contract_form_data(form)
    update_contract(contract_id, data)

    if files:
        add_files(contract_id, files, user)

    return RedirectResponse(url=router.url_path_for("edit_contract_page", contract_id=contract_id), status_code=303)

@router.post("/contracts/delete")
async def delete_contract(
    request: Request,
    contract_id: str = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    init_db()
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/contracts")
    # 刪主檔，附件與版本因 FK 設計自動刪除
    delete_contract_row(contract_id)
    # 同步刪掉實體檔案
    for f in list_files(contract_id):
        try:
            os.remove(os.path.join(CONTRACT_ATTACHMENT_DIR, f["stored_name"]))
        except FileNotFoundError:
            pass
    return RedirectResponse(url=router.url_path_for("manage_contracts_page"), status_code=303)

# 附件：刪除（需 manage）
@router.post("/contracts/files/{file_id}/delete")
async def delete_contract_file(
    file_id: int,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")
    ctx = get_base_context(request, user, role, permissions)
    if not _check_perm(ctx, need="manage"):
        return RedirectResponse(url="/contracts")
    delete_file(file_id)
    referer = request.headers.get("referer") or "/contracts"
    return RedirectResponse(url=referer, status_code=303)

# 附件：下載（需 view）
@router.get("/contracts/files/{file_id}")
async def download_contract_file(
    file_id: int,
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
    row = get_file(file_id)
    if not row: return HTMLResponse("File Not Found", status_code=404)
    path = os.path.join(CONTRACT_ATTACHMENT_DIR, row["stored_name"])
    if not os.path.exists(path): return HTMLResponse("File Not Found", status_code=404)
    return FileResponse(path, filename=row["orig_name"] or os.path.basename(path))
