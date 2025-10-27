# routers/approvals_router.py
from fastapi import APIRouter, Request, Cookie, Form, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from typing import Optional, List, Tuple, Set, Dict, Any
import sqlite3
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import smtplib
from email.message import EmailMessage
import json
from pathlib import Path
from uuid import uuid4
import re
import logging  # <-- 新增這一行

from app_utils import get_base_context, templates

# ========= 路徑與 DB =========
BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE_DIR / "data" / "approvals.db"
DB_PATH = os.getenv("APPROVALS_DB", str(DEFAULT_DB))

TAIPEI = ZoneInfo("Asia/Taipei")

router = APIRouter(prefix="/approvals", tags=["Approvals"])

UPLOAD_ROOT = BASE_DIR / "data" / "uploads" / "approvals"


def _safe_filename(name: str) -> str:
    base = Path(str(name or "file")).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row  # ✅ 允許用欄位名稱取值
    return conn


# ========= 欄位/資料表自癒 =========
def ensure_confidential_column():
    with get_conn() as conn:
        c = conn.cursor()
        cols = [
            r["name"].lower()
            for r in c.execute("PRAGMA table_info(approvals)").fetchall()
        ]
        if "confidential" not in cols:
            c.execute("ALTER TABLE approvals ADD COLUMN confidential TEXT")
            conn.commit()


def ensure_publish_memo_column():
    with get_conn() as conn:
        c = conn.cursor()
        cols = [
            r["name"].lower()
            for r in c.execute("PRAGMA table_info(approvals)").fetchall()
        ]
        if "publish_memo" not in cols:
            c.execute(
                "ALTER TABLE approvals ADD COLUMN publish_memo INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()


def ensure_view_scope_column():
    with get_conn() as conn:
        c = conn.cursor()
        cols = [
            r["name"].lower()
            for r in c.execute("PRAGMA table_info(approvals)").fetchall()
        ]
        if "view_scope" not in cols:
            c.execute(
                "ALTER TABLE approvals ADD COLUMN view_scope TEXT NOT NULL DEFAULT 'restricted'"
            )
            conn.commit()


def ensure_memos_table():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
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
        """
        )
        conn.commit()


# ========= 小工具 =========
def now_iso():
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def _format_taipei(value: Optional[str]) -> str:
    if not value:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except Exception:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
        return s
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


def _combine_label(english_name: str, name: str) -> str:
    a = str(english_name or "").strip()
    b = str(name or "").strip()
    if a and b:
        return f"{a} {b}"
    return a or b


def _norm(s: str) -> str:
    return " ".join(str(s or "").split())


def _normi(s: str) -> str:
    return _norm(s).casefold()


def _normalize_scope(raw_scope: Optional[str]) -> str:
    scope = str(raw_scope or "restricted").strip().lower()
    if scope in {"org", "restricted", "top_secret"}:
        return scope
    return "restricted"


# ====== 修復工具：重新編號 step_order ======
def _renumber_steps(approval_id: int):
    with get_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id FROM approval_steps WHERE approval_id=? ORDER BY step_order, id",
            (approval_id,),
        ).fetchall()
        for idx, r in enumerate(rows):
            sid = r["id"]
            c.execute("UPDATE approval_steps SET step_order=? WHERE id=?", (idx, sid))
        conn.commit()


def _renumber_all_steps():
    with get_conn() as conn:
        c = conn.cursor()
        ids = c.execute("SELECT DISTINCT approval_id FROM approval_steps").fetchall()
    for r in ids:
        _renumber_steps(r["approval_id"])


def _log_action(conn, approval_id, actor, action, note="", step_id=None):
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO approval_actions(approval_id, step_id, actor, action, note, created_at)
        VALUES(?,?,?,?,?,?)
        """,
        (approval_id, step_id, actor, action, note, now_iso()),
    )


def _build_approval_snapshot(conn, approval_id):
    c = conn.cursor()
    row = c.execute(
        """
        SELECT id, subject, description, confidential, requester, requester_dept,
               status, current_step, view_scope, publish_memo, submitted_at
        FROM approvals WHERE id=?
        """,
        (approval_id,),
    ).fetchone()
    if not row:
        return None

    attachments = c.execute(
        """
        SELECT id, orig_name, stored_name, content_type, size_bytes, uploaded_by, uploaded_at
        FROM approval_files
        WHERE approval_id=?
        ORDER BY id
        """,
        (approval_id,),
    ).fetchall()

    return {
        "approval": {
            "id": row["id"],
            "subject": row["subject"],
            "description": row["description"],
            "confidential": row["confidential"],
            "requester": row["requester"],
            "requester_dept": row["requester_dept"],
            "status": row["status"],
            "current_step": row["current_step"],
            "view_scope": row["view_scope"],
            "publish_memo": row["publish_memo"],
            "submitted_at": row["submitted_at"],
        },
        "attachments": [
            {
                "id": att["id"],
                "name": att["orig_name"],
                "stored": att["stored_name"],
                "content_type": att["content_type"],
                "size_bytes": att["size_bytes"],
                "uploaded_by": att["uploaded_by"],
                "uploaded_at": att["uploaded_at"],
            }
            for att in attachments
        ],
    }


def _record_version(conn, approval_id, actor, change_type, detail=None):
    snapshot = _build_approval_snapshot(conn, approval_id)
    if not snapshot:
        return

    c = conn.cursor()
    current = c.execute(
        "SELECT COALESCE(MAX(version), 0) FROM approval_versions WHERE approval_id=?",
        (approval_id,),
    ).fetchone()
    next_ver = (current[0] if current and current[0] is not None else 0) + 1

    change_payload = {"type": change_type}
    if detail:
        change_payload["detail"] = detail

    c.execute(
        """
        INSERT INTO approval_versions(approval_id, version, snapshot, changes, actor, created_at)
        VALUES(?,?,?,?,?,?)
        """,
        (
            approval_id,
            next_ver,
            json.dumps(snapshot, ensure_ascii=False),
            json.dumps(change_payload, ensure_ascii=False),
            actor,
            now_iso(),
        ),
    )


# ========= 建表（若不存在）=========
def ensure_employees_table():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS employees(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder TEXT,
            cost_center TEXT,
            department TEXT,
            department_1 TEXT,
            team TEXT,
            job_title TEXT,
            name TEXT,
            employee_id TEXT,
            english_name TEXT,
            email TEXT,
            extension_number TEXT
        )
        """
        )
        conn.commit()


def ensure_db():
    """確保簽核相關表存在；建立前後做自癒，避免索引建立失敗"""
    with get_conn() as conn:
        c = conn.cursor()
        # 主表
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            description TEXT,
            confidential TEXT,
            requester TEXT NOT NULL,
            requester_dept TEXT,
            submitted_at TEXT NOT NULL,
            status TEXT NOT NULL,          -- pending / approved / rejected
            current_step INTEGER NOT NULL, -- 0-based；完成/退回為 -1
            view_scope TEXT NOT NULL DEFAULT 'restricted',
            publish_memo INTEGER NOT NULL DEFAULT 0
        )
        """
        )
        # 關卡表
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approval_steps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id INTEGER NOT NULL,
            step_order INTEGER NOT NULL,          -- 0,1,2...
            approver_name TEXT NOT NULL,
            approver_email TEXT,
            status TEXT NOT NULL,                 -- pending / approved / rejected
            decided_at TEXT,
            comment TEXT,
            FOREIGN KEY(approval_id) REFERENCES approvals(id) ON DELETE CASCADE
        )
        """
        )
        # 歷程
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approval_actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id INTEGER NOT NULL,
            step_id INTEGER,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(approval_id) REFERENCES approvals(id) ON DELETE CASCADE,
            FOREIGN KEY(step_id) REFERENCES approval_steps(id)
        )
        """
        )
        # 附件
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approval_files(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id INTEGER NOT NULL,
            orig_name TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER,
            uploaded_by TEXT,
            uploaded_at TEXT,
            FOREIGN KEY(approval_id) REFERENCES approvals(id) ON DELETE CASCADE
        )
        """
        )
        # 版本紀錄
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approval_versions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            snapshot TEXT NOT NULL,
            changes TEXT,
            actor TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(approval_id) REFERENCES approvals(id) ON DELETE CASCADE,
            UNIQUE(approval_id, version)
        )
        """
        )
        conn.commit()

    # 舊庫補欄位
    ensure_view_scope_column()
    ensure_confidential_column()
    ensure_publish_memo_column()

    # 自癒 + 索引
    try:
        _renumber_all_steps()
    except Exception as e:
        print("[approvals] renumber all steps failed:", e)

    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_steps_order
                ON approval_steps(approval_id, step_order)
                """
            )
            conn.commit()
    except sqlite3.IntegrityError:
        _renumber_all_steps()
        try:
            with get_conn() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_steps_order
                    ON approval_steps(approval_id, step_order)
                """
                )
                conn.commit()
        except sqlite3.IntegrityError as e:
            print("[approvals] WARN: create unique index failed after renumber:", e)


# ========= 從 employees 取資料 =========
def list_departments() -> List[str]:
    ensure_employees_table()
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT TRIM(x) AS d
                FROM (
                    SELECT department AS x FROM employees
                    UNION ALL
                    SELECT department_1 AS x FROM employees
                )
                WHERE TRIM(COALESCE(x,'')) <> ''
                ORDER BY d
            """
            ).fetchall()
        return [r["d"] for r in rows]
    except Exception as e:
        print("[approvals] list_departments error:", e)
        return []


def names_by_dept(dept: str) -> List[str]:
    if not dept:
        return []
    ensure_employees_table()
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT english_name, name
                FROM employees
                WHERE TRIM(COALESCE(department,'')) = TRIM(?)
                   OR TRIM(COALESCE(department_1,'')) = TRIM(?)
            """,
                (dept, dept),
            ).fetchall()
    except Exception as e:
        print("[approvals] names_by_dept error:", e)
        return []

    seen, labels = set(), []
    for r in rows:
        en, nm = r["english_name"], r["name"]
        label = _combine_label(en, nm)
        if label and label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def _find_employee_by_display(display_name: str) -> Optional[Tuple[str, str, str]]:
    ensure_employees_table()
    target = _normi(display_name)
    if not target:
        return None
    try:
        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT english_name, name, email
                FROM employees
                WHERE TRIM(COALESCE(english_name,'')) <> ''
                   OR TRIM(COALESCE(name,'')) <> ''
            """
            ).fetchall()
    except Exception as e:
        print("[approvals] _find_employee_by_display error:", e)
        return None

    for r in rows:
        en, nm, mail = r["english_name"], r["name"], r["email"]
        en_n = _normi(en)
        nm_n = _normi(nm)
        combo = _normi(_combine_label(en, nm))
        if target in {en_n, nm_n, combo}:
            return (en or "", nm or "", (mail or "").strip())
    return None


def email_by_name(any_name: str) -> Optional[str]:
    found = _find_employee_by_display(any_name)
    if not found:
        return None
    _, _, mail = found
    return mail if ("@" in (mail or "")) else None


def user_identity_keys(login_user: str) -> Set[str]:
    keys: Set[str] = set()
    u = _normi(login_user)
    if u:
        keys.add(u)

    ensure_employees_table()
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT english_name, name, email FROM employees"
            ).fetchall()
    except Exception:
        rows = []

    for r in rows:
        en, nm, mail = r["english_name"], r["name"], r["email"]
        combo = _combine_label(en, nm)
        local = mail.split("@")[0] if mail else ""
        forms_n = {_normi(x) for x in [en, nm, combo, local] if x}
        if u in forms_n:
            keys |= forms_n

    return {k for k in keys if k}


# ========= Mail =========
def send_mail_safe(to_list: List[str], subject: str, body: str):
    to_list = [t for t in (to_list or []) if t and "@" in t]
    if not to_list:
        print(f"[MAIL] 跳過寄送（沒有可用信箱）。subj={subject}")
        return

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    sender = os.getenv("SMTP_FROM", user or "no-reply@example.com")

    if not (host and user and pwd):
        print(
            f"[MAIL] 未設定 SMTP_HOST/USER/PASS，僅列印通知：to={to_list}, subj={subject}\n{body}"
        )
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)


# ========= 權限 =========
def can_use_approvals(role: Optional[str], permissions: Optional[str]) -> bool:
    if role == "admin":
        return True
    try:
        perms = json.loads(permissions) if permissions else {}
        return bool(perms.get("approvals", False))
    except Exception:
        return False


def _is_requester_or_admin(approval_id: int, user: str, role: Optional[str]) -> bool:
    if role == "admin":
        return True
    with get_conn() as conn:
        r = conn.execute(
            "SELECT requester FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        return bool(r and r["requester"] == user)


def _user_involved_in_approval(approval_id: int, user: str) -> bool:
    keys = user_identity_keys(user)
    with get_conn() as conn:
        c = conn.cursor()
        r = c.execute(
            "SELECT requester FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if not r:
            return False
        if _normi(r["requester"]) in keys:
            return True
        rows = c.execute(
            "SELECT approver_name FROM approval_steps WHERE approval_id=?",
            (approval_id,),
        ).fetchall()
        for r2 in rows:
            if _normi(r2["approver_name"]) in keys:
                return True
    return False


def can_view_approval(
    approval_id: int, user: str, role: Optional[str], permissions: Optional[str]
) -> bool:
    if role == "admin":
        return True
    if not can_use_approvals(role, permissions):
        return False
    with get_conn() as conn:
        r = conn.execute(
            "SELECT view_scope FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if not r:
            return False
        scope = (r["view_scope"] or "restricted").strip().lower()
    if scope == "org":
        return True
    return _user_involved_in_approval(approval_id, user)


# ========= 頁面：送簽表單 =========
@router.get("/new", response_class=HTMLResponse)
async def new_form(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ctx = get_base_context(request, user, role, permissions)
    ctx["departments"] = list_departments()
    return templates.TemplateResponse("approvals/new.html", ctx)


# AJAX：依部門取可選簽核人
@router.get("/approvers")
async def api_approvers(dept: str):
    return {"dept": dept, "names": names_by_dept(dept)}


# ========= 送出簽核單 =========
@router.post("/create")
async def create_approval(
    request: Request,
    subject: str = Form(...),
    description: str = Form(""),
    requester: str = Form(...),
    requester_dept: str = Form(""),
    view_scope: str = Form("restricted"),  # org | restricted
    approver_chain: str = Form(...),  # JSON：["A B", "..."]
    files: List[UploadFile] = File([]),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    confidential: str = Form(""),
    publish_memo: str = Form("0"),  # ✅ 新增
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()
    try:
        chain = json.loads(approver_chain)
        if not isinstance(chain, list):
            chain = []
    except Exception:
        chain = []

    if not chain:
        return JSONResponse(
            {"ok": False, "error": "請至少選擇一位串簽人員"}, status_code=400
        )

    pm = 1 if str(publish_memo).strip().lower() in ("1", "true", "on", "yes") else 0

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO approvals(
                subject, description, confidential, requester, requester_dept,
                submitted_at, status, current_step, view_scope, publish_memo
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                subject,
                description,
                confidential,
                requester,
                requester_dept,
                now_iso(),
                "pending",
                0,
                _normalize_scope(view_scope),
                pm,
            ),
        )
        approval_id = c.lastrowid

        # 建立 steps
        for idx, disp in enumerate(chain):
            emp = _find_employee_by_display(disp)
            if emp:
                en, nm, mail = emp
                save_name = nm or en or disp
                save_mail = mail if ("@" in (mail or "")) else None
            else:
                save_name = (
                    str(disp).trim()
                    if hasattr(str(disp), "trim")
                    else str(disp).strip()
                )
                save_mail = None

            c.execute(
                """
            INSERT INTO approval_steps(approval_id, step_order, approver_name, approver_email, status)
            VALUES(?,?,?,?,?)
            """,
                (approval_id, idx, save_name, save_mail, "pending"),
            )

        # 動作紀錄
        c.execute(
            """
        INSERT INTO approval_actions(approval_id, step_id, actor, action, note, created_at)
        VALUES(?,?,?,?,?,?)
        """,
            (approval_id, None, requester, "submit", "", now_iso()),
        )
        conn.commit()

    # ===== 儲存附件 =====
    if files:
        ensure_db()
        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        updir = UPLOAD_ROOT / str(approval_id)
        updir.mkdir(parents=True, exist_ok=True)

        with get_conn() as conn:
            c = conn.cursor()
            for uf in files:
                if not uf or not uf.filename:
                    continue
                orig = _safe_filename(uf.filename)
                ext = "".join(Path(orig).suffixes)
                stored = f"{uuid4().hex}{ext}"
                fullpath = updir / stored

                size = 0
                while True:
                    chunk = await uf.read(1024 * 1024)
                    if not chunk:
                        break
                    with open(fullpath, "ab") as f:
                        f.write(chunk)
                    size += len(chunk)
                await uf.close()

                c.execute(
                    """
                    INSERT INTO approval_files(approval_id, orig_name, stored_name, content_type, size_bytes, uploaded_by, uploaded_at)
                    VALUES(?,?,?,?,?,?,?)
                """,
                    (
                        approval_id,
                        orig,
                        stored,
                        uf.content_type or "",
                        size,
                        requester,
                        now_iso(),
                    ),
                )
            conn.commit()

    with get_conn() as conn:
        _record_version(
            conn, approval_id, requester, "submit", {"note": "initial submission"}
        )

    # 通知第一關
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """SELECT approver_name, approver_email FROM approval_steps
                     WHERE approval_id=? AND step_order=0""",
            (approval_id,),
        )
        row = c.fetchone()
        if row:
            name, mail = row["approver_name"], row["approver_email"]
            send_mail_safe(
                [mail] if mail else [],
                f"【簽核通知】請審核：{subject}",
                f"{name} 您好：\n\n有一筆文件等待您審核。\n主旨：{subject}\n說明：{description}\n送簽人：{requester}\n\n請進入系統 /approvals 查看並簽核。",
            )

    return RedirectResponse(url=f"/approvals", status_code=303)


# ========= 清單：我的待簽 / 我送簽 =========
@router.get("", response_class=HTMLResponse)
async def list_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()
    keys = user_identity_keys(user)

    with get_conn() as conn:
        c = conn.cursor()
        # 取所有待簽（目前關卡）
        c.execute(
            """
        SELECT a.id, a.subject, a.requester, a.submitted_at, s.step_order, s.approver_name
        FROM approvals a
        JOIN approval_steps s ON a.id = s.approval_id
        WHERE s.status = 'pending'
          AND a.status = 'pending'
          AND a.current_step = s.step_order
        ORDER BY a.submitted_at DESC
        """
        )
        rows = c.fetchall()
        my_todo = []
        for r in rows:
            appr_name = r["approver_name"]
            if _normi(appr_name) in keys:
                my_todo.append(
                    dict(
                        id=r["id"],
                        subject=r["subject"],
                        requester=r["requester"],
                        submitted_at=_format_taipei(r["submitted_at"]),
                        step=r["step_order"],
                    )
                )

        # 我送簽
        c.execute(
            """
        SELECT id, subject, status, submitted_at, current_step FROM approvals
        WHERE requester = ?
        ORDER BY submitted_at DESC
        """,
            (user,),
        )
        my_sent = [
            dict(
                id=r["id"],
                subject=r["subject"],
                status=r["status"],
                submitted_at=_format_taipei(r["submitted_at"]),
                current_step=r["current_step"],
            )
            for r in c.fetchall()
        ]

    ctx = get_base_context(request, user, role, permissions)
    ctx["my_todo"] = my_todo
    ctx["my_sent"] = my_sent
    return templates.TemplateResponse("approvals/list.html", ctx)


# ========= 單筆詳情 =========
@router.get("/{approval_id}", response_class=HTMLResponse)
async def detail_page(
    approval_id: int,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()
    ensure_publish_memo_column()  # ✅ 保險：舊庫補欄位
    if not can_view_approval(approval_id, user, role, permissions):
        return HTMLResponse("你沒有權限檢視此簽核單。", status_code=403)
    _renumber_steps(approval_id)

    with get_conn() as conn:
        c = conn.cursor()

        # 主檔
        c.execute(
            """
            SELECT id, subject, description, confidential, requester, requester_dept,
                   status, current_step, submitted_at, view_scope, publish_memo
            FROM approvals WHERE id=?
        """,
            (approval_id,),
        )
        a = c.fetchone()
        if not a:
            return HTMLResponse("Not found", status_code=404)
        approval = dict(
            id=a["id"],
            subject=a["subject"],
            description=a["description"],
            confidential=a["confidential"],
            requester=a["requester"],
            requester_dept=a["requester_dept"],
            status=a["status"],
            current_step=a["current_step"],
            submitted_at=_format_taipei(a["submitted_at"]),
            view_scope=_normalize_scope(a["view_scope"]),
            publish_memo=int(a["publish_memo"] or 0),  # ✅ 提供前端顯示唯讀開關
        )

        # 關卡
        c.execute(
            """
            SELECT id, step_order, approver_name, approver_email, status, decided_at, comment
            FROM approval_steps
            WHERE approval_id=? ORDER BY step_order
        """,
            (approval_id,),
        )
        steps = [
            dict(
                id=r["id"],
                step_order=r["step_order"],
                approver_name=r["approver_name"],
                approver_email=r["approver_email"],
                status=r["status"],
                decided_at=_format_taipei(r["decided_at"]),
                comment=r["comment"],
            )
            for r in c.fetchall()
        ]

        # 歷程
        c.execute(
            """
            SELECT actor, action, note, created_at, step_id
            FROM approval_actions
            WHERE approval_id=? ORDER BY id
        """,
            (approval_id,),
        )
        actions = [
            dict(
                actor=r["actor"],
                action=r["action"],
                note=r["note"],
                created_at=_format_taipei(r["created_at"]),
                step_id=r["step_id"],
            )
            for r in c.fetchall()
        ]
        # 每個 step 最新一次動作（id 遞增 → 後者覆蓋前者）
        latest_actions = {}
        for a in actions:
            sid = a.get("step_id")
            if sid:
                latest_actions[sid] = a["action"]

        # 附件
        c.execute(
            """
            SELECT id, orig_name, content_type, size_bytes, uploaded_by, uploaded_at
            FROM approval_files
            WHERE approval_id=?
            ORDER BY id
        """,
            (approval_id,),
        )
        attachments = [
            dict(
                id=r["id"],
                name=r["orig_name"],
                ctype=r["content_type"],
                size=r["size_bytes"],
                by=r["uploaded_by"],
                at=_format_taipei(r["uploaded_at"]),
            )
            for r in c.fetchall()
        ]

    # 我是否可以簽
    keys = user_identity_keys(user)
    cur_obj = None
    can_act = False
    if approval["status"] == "pending" and approval["current_step"] >= 0:
        for s in steps:
            if s["step_order"] == approval["current_step"]:
                cur_obj = s
                if s["status"] == "pending" and _normi(s["approver_name"]) in keys:
                    can_act = True
                break

    can_edit_content = approval["status"] == "pending" and (
        (role == "admin") or (approval["requester"] == user)
    )
    user_involved = bool(user) and _user_involved_in_approval(approval_id, user)
    scope_value = _normalize_scope(approval["view_scope"])
    can_view_confidential = True
    if scope_value == "org" and role != "admin" and not user_involved:
        can_view_confidential = False
    elif scope_value == "top_secret" and role != "admin" and not user_involved:
        can_view_confidential = False

    ctx = get_base_context(request, user, role, permissions)
    ctx["approval"] = approval
    ctx["steps"] = steps
    ctx["actions"] = actions
    ctx["latest_actions"] = latest_actions  # ← 新增這行
    ctx["can_act"] = can_act
    ctx["current_step_obj"] = cur_obj
    ctx["attachments"] = attachments
    ctx["departments"] = list_departments()
    ctx["can_manage_chain"] = can_edit_content
    ctx["can_edit_content"] = can_edit_content
    ctx["can_manage_attachments"] = can_edit_content
    ctx["can_view_confidential"] = can_view_confidential
    ctx["confidential_value"] = (
        approval["confidential"] if can_view_confidential else ""
    )
    ctx["role"] = role  # 給模板判斷 admin 按鈕
    return templates.TemplateResponse("approvals/detail.html", ctx)


@router.post("/{approval_id}/edit")
async def update_approval_details(
    approval_id: int,
    subject: str = Form(...),
    description: str = Form(""),
    confidential: str = Form(""),
    view_scope: str = Form("restricted"),
    publish_memo: str = Form("0"),
    requester_dept: str = Form(""),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()

    with get_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            """
            SELECT subject, description, confidential, requester, status,
                   view_scope, publish_memo, requester_dept
            FROM approvals WHERE id=?
            """,
            (approval_id,),
        ).fetchone()
        if not row:
            return HTMLResponse("Not found", status_code=404)

        if row["status"] != "pending":
            return RedirectResponse(
                url=f"/approvals/{approval_id}?error=not_editable",
                status_code=303,
            )

        if role != "admin" and row["requester"] != user:
            return HTMLResponse("你沒有權限編輯此簽核單。", status_code=403)

        normalized_view_scope = _normalize_scope(view_scope)
        pm_value = (
            1 if str(publish_memo).strip().lower() in ("1", "true", "on", "yes") else 0
        )

        changes = []
        if row["subject"] != subject:
            changes.append("主旨")
        if (row["description"] or "") != description:
            changes.append("說明")
        if (row["confidential"] or "") != confidential:
            changes.append("Confidential")
        if row["view_scope"] != normalized_view_scope:
            changes.append("可見範圍")
        if int(row["publish_memo"] or 0) != pm_value:
            changes.append("公告設定")
        if (row["requester_dept"] or "") != requester_dept:
            changes.append("申請人部門")

        if not changes:
            return RedirectResponse(url=f"/approvals/{approval_id}", status_code=303)

        c.execute(
            """
            UPDATE approvals
            SET subject=?, description=?, confidential=?, view_scope=?, publish_memo=?, requester_dept=?
            WHERE id=?
            """,
            (
                subject,
                description,
                confidential,
                normalized_view_scope,
                pm_value,
                requester_dept,
                approval_id,
            ),
        )
        _log_action(
            conn,
            approval_id,
            user,
            "update_content",
            f"更新內容：{', '.join(changes)}",
        )
        _record_version(
            conn,
            approval_id,
            user,
            "update_content",
            {"fields": changes},
        )

    return RedirectResponse(url=f"/approvals/{approval_id}?ok=1", status_code=303)


@router.post("/{approval_id}/attachments")
async def add_approval_attachments(
    approval_id: int,
    files: List[UploadFile] = File([]),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()

    valid_files = [f for f in (files or []) if f and f.filename]
    if not valid_files:
        return RedirectResponse(
            url=f"/approvals/{approval_id}?error=no_files",
            status_code=303,
        )

    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    updir = UPLOAD_ROOT / str(approval_id)
    updir.mkdir(parents=True, exist_ok=True)

    added: List[str] = []

    with get_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT status, requester FROM approvals WHERE id=?",
            (approval_id,),
        ).fetchone()
        if not row:
            return HTMLResponse("Not found", status_code=404)
        if row["status"] != "pending":
            return RedirectResponse(
                url=f"/approvals/{approval_id}?error=not_editable",
                status_code=303,
            )
        if role != "admin" and row["requester"] != user:
            return HTMLResponse("你沒有權限編輯附件。", status_code=403)

        for uf in valid_files:
            orig = _safe_filename(uf.filename)
            ext = "".join(Path(orig).suffixes)
            stored = f"{uuid4().hex}{ext}"
            fullpath = updir / stored
            size = 0
            with open(fullpath, "wb") as fh:
                while True:
                    chunk = await uf.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    size += len(chunk)
            await uf.close()
            c.execute(
                """
                INSERT INTO approval_files(approval_id, orig_name, stored_name, content_type, size_bytes, uploaded_by, uploaded_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    approval_id,
                    orig,
                    stored,
                    uf.content_type or "",
                    size,
                    user,
                    now_iso(),
                ),
            )
            added.append(orig)

        if added:
            _log_action(
                conn,
                approval_id,
                user,
                "add_attachment",
                f"新增附件：{', '.join(added)}",
            )
            _record_version(
                conn,
                approval_id,
                user,
                "add_attachment",
                {"files_added": added},
            )

    return RedirectResponse(url=f"/approvals/{approval_id}?ok=1", status_code=303)


@router.post("/{approval_id}/attachments/{file_id}/delete")
async def delete_approval_attachment(
    approval_id: int,
    file_id: int,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()

    with get_conn() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT status, requester FROM approvals WHERE id=?",
            (approval_id,),
        ).fetchone()
        if not row:
            return HTMLResponse("Not found", status_code=404)
        if row["status"] != "pending":
            return RedirectResponse(
                url=f"/approvals/{approval_id}?error=not_editable",
                status_code=303,
            )
        if role != "admin" and row["requester"] != user:
            return HTMLResponse("你沒有權限編輯附件。", status_code=403)

        file_row = c.execute(
            """
            SELECT orig_name, stored_name
            FROM approval_files
            WHERE id=? AND approval_id=?
            """,
            (file_id, approval_id),
        ).fetchone()
        if not file_row:
            return HTMLResponse("附件不存在。", status_code=404)

        c.execute(
            "DELETE FROM approval_files WHERE id=? AND approval_id=?",
            (file_id, approval_id),
        )

        file_path = UPLOAD_ROOT / str(approval_id) / file_row["stored_name"]
        if file_path.exists():
            try:
                file_path.unlink()
            except Exception:
                pass

        removed = file_row["orig_name"]
        _log_action(
            conn,
            approval_id,
            user,
            "delete_attachment",
            f"刪除附件：{removed}",
        )
        _record_version(
            conn,
            approval_id,
            user,
            "delete_attachment",
            {"files_removed": [removed]},
        )

    return RedirectResponse(url=f"/approvals/{approval_id}?ok=1", status_code=303)


# ========= 簽核動作（同意 / 退回 / 知會） =========
@router.post("/{approval_id}/action")
async def do_action(
    approval_id: int,
    action: str = Form(...),  # approve / reject / ack(知會)
    comment: str = Form(""),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()

    # ack 可不填意見，預設寫「已知會」
    is_ack = action == "ack"
    if not is_ack and not (comment or "").strip():
        return RedirectResponse(
            url=f"/approvals/{approval_id}?error=comment_required", status_code=303
        )
    comment = (comment or "").strip() or ("已知會" if is_ack else "")

    with get_conn() as conn:
        c = conn.cursor()
        # 目前 step + 主檔資訊（含 publish_memo）
        c.execute(
            "SELECT current_step, subject, description, requester, view_scope, publish_memo "
            "FROM approvals WHERE id=?",
            (approval_id,),
        )
        row = c.fetchone()
        if not row:
            return HTMLResponse("Not found", status_code=404)
        cur_step = row["current_step"]
        subject = row["subject"]
        desc = row["description"]
        requester = row["requester"]
        view_scope_v = row["view_scope"]
        publish_memo_flag = int(row["publish_memo"] or 0)  # ← 你原本就有的欄位與邏輯

        # 當前 pending 關
        c.execute(
            """
            SELECT id, approver_name, approver_email
            FROM approval_steps
            WHERE approval_id=? AND step_order=? AND status='pending'
            """,
            (approval_id, cur_step),
        )
        s = c.fetchone()
        if not s:
            return HTMLResponse("無可簽核的關卡（可能已被他人處理）", status_code=400)
        step_id, approver_name, approver_email = (
            s["id"],
            s["approver_name"],
            s["approver_email"],
        )

        # 身分授權
        keys = user_identity_keys(user)
        if _normi(approver_name) not in keys:
            return HTMLResponse("您不是此關簽核人", status_code=403)

        # 狀態決策：ack 與 approve 一樣視為通過；reject 照舊
        if action in ("approve", "ack"):
            new_status = "approved"
        elif action == "reject":
            new_status = "rejected"
        else:
            return HTMLResponse("不支援的動作", status_code=400)

        # 更新步驟狀態
        c.execute(
            """UPDATE approval_steps
               SET status=?, decided_at=?, comment=?
               WHERE id=?""",
            (new_status, now_iso(), comment, step_id),
        )

        # 歷程
        c.execute(
            """INSERT INTO approval_actions(approval_id, step_id, actor, action, note, created_at)
               VALUES(?,?,?,?,?,?)""",
            (approval_id, step_id, user, action, comment, now_iso()),
        )

        if action == "reject":
            # 整體退回
            c.execute(
                "UPDATE approvals SET status=?, current_step=? WHERE id=?",
                ("rejected", -1, approval_id),
            )
            conn.commit()
            # 通知申請人
            send_mail_safe(
                [email_by_name(requester)] if email_by_name(requester) else [],
                f"【簽核退回】{subject}",
                f"{requester} 您好：\n\n主旨：{subject}\n已被 {user} 退回。\n意見：{comment}\n\n請登入系統查看詳情。",
            )
            return RedirectResponse(url=f"/approvals/{approval_id}", status_code=303)

        # 同意 / 知會 → 前進或完成（沿用你原本的推進與 memo 發佈）
        c.execute(
            "SELECT COUNT(*) AS cnt FROM approval_steps WHERE approval_id=?",
            (approval_id,),
        )
        total_steps = c.fetchone()["cnt"]
        next_step = cur_step + 1

        if next_step >= total_steps:
            # 全部完成
            c.execute(
                "UPDATE approvals SET status=?, current_step=? WHERE id=?",
                ("approved", -1, approval_id),
            )
            conn.commit()

            # （保持與你現有一致）若開啟 publish_memo，發佈 Memo
            if publish_memo_flag == 1:
                ensure_memos_table()
                with get_conn() as conn3:
                    c3 = conn3.cursor()
                    visibility = _normalize_scope(view_scope_v)
                    if visibility == "top_secret":
                        visibility = "restricted"
                    c3.execute(
                        """
                        INSERT INTO memos(title, body, visibility, created_at, author, source, source_id)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            subject,
                            desc,  # 直接帶「說明」
                            visibility,  # org/restricted；top_secret 降到 restricted
                            now_iso(),
                            requester,
                            "approvals",
                            approval_id,
                        ),
                    )
                    conn3.commit()

            # 通知申請人完成
            send_mail_safe(
                [email_by_name(requester)] if email_by_name(requester) else [],
                f"【簽核完成】{subject}",
                f"{requester} 您好：\n\n主旨：{subject}\n所有關卡已完成簽核。\n\n請登入系統查看。",
            )
        else:
            # 推進下一關，並通知下一位簽核人
            c.execute(
                "UPDATE approvals SET current_step=? WHERE id=?",
                (next_step, approval_id),
            )
            conn.commit()
            c.execute(
                """SELECT approver_name, approver_email FROM approval_steps
                   WHERE approval_id=? AND step_order=?""",
                (approval_id, next_step),
            )
            nx = c.fetchone()
            if nx:
                nx_name, nx_mail = nx["approver_name"], nx["approver_email"]
                send_mail_safe(
                    [nx_mail] if nx_mail else [],
                    f"【簽核通知】請審核：{subject}",
                    f"{nx_name} 您好：\n\n有一筆文件等待您審核。\n主旨：{subject}\n說明：{desc}\n送簽人：{requester}\n\n請進入系統 /approvals 查看並簽核。",
                )

    return RedirectResponse(url=f"/approvals/{approval_id}", status_code=303)


# ========= 新增簽核人員 =========
@router.post("/{approval_id}/steps/add")
async def add_approver_step(
    approval_id: int,
    display_name: str = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return JSONResponse(
            {"ok": False, "error": "permission_denied"}, status_code=403
        )
    if not _is_requester_or_admin(approval_id, user, role):
        return JSONResponse(
            {"ok": False, "error": "only_requester_or_admin"}, status_code=403
        )

    ensure_db()
    disp = (display_name or "").strip()
    if not disp:
        return JSONResponse(
            {"ok": False, "error": "invalid_display_name"}, status_code=400
        )

    emp = _find_employee_by_display(disp)
    if emp:
        en, nm, mail = emp
        save_name = nm or en or disp
        save_mail = mail if ("@" in (mail or "")) else None
    else:
        save_name = disp
        save_mail = None

    with get_conn() as conn:
        c = conn.cursor()
        # 檢查整體狀態
        r = c.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if not r:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if r["status"] != "pending":
            return JSONResponse(
                {"ok": False, "error": "approval_not_pending"}, status_code=400
            )

        # 去重：同名不可再加入
        rows = c.execute(
            "SELECT approver_name FROM approval_steps WHERE approval_id=?",
            (approval_id,),
        ).fetchall()
        names_n = {_normi(x["approver_name"]) for x in rows if x and x["approver_name"]}
        if _normi(save_name) in names_n:
            return JSONResponse(
                {"ok": False, "error": "duplicated_name"}, status_code=400
            )

        # 取最後一關序號
        r = c.execute(
            "SELECT MAX(step_order) AS mx FROM approval_steps WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        next_order = (int(r["mx"]) + 1) if (r and r["mx"] is not None) else 0

        # 寫入
        c.execute(
            """INSERT INTO approval_steps(approval_id, step_order, approver_name, approver_email, status)
               VALUES(?,?,?,?, 'pending')""",
            (approval_id, next_order, save_name, save_mail),
        )
        step_id = c.lastrowid

        # 歷程
        c.execute(
            """INSERT INTO approval_actions(approval_id, step_id, actor, action, note, created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                approval_id,
                step_id,
                user,
                "add_approver",
                f"新增簽核人：{save_name}",
                now_iso(),
            ),
        )
        conn.commit()

    _renumber_steps(approval_id)
    return JSONResponse({"ok": True})


# ========= 重新排序 =========
@router.post("/{approval_id}/steps/reorder")
async def reorder_pending_steps(
    approval_id: int,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return JSONResponse(
            {"ok": False, "error": "permission_denied"}, status_code=403
        )
    if not _is_requester_or_admin(approval_id, user, role):
        return JSONResponse(
            {"ok": False, "error": "only_requester_or_admin"}, status_code=403
        )

    try:
        body = await request.json()
        new_order_ids = list(map(int, body.get("order", [])))
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    SHIFT = 100000  # 避免唯一索引衝突的位移

    with get_conn() as conn:
        c = conn.cursor()
        r = c.execute(
            "SELECT status, current_step FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if not r:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if r["status"] != "pending":
            return JSONResponse(
                {"ok": False, "error": "approval_not_pending"}, status_code=400
            )
        cur_step = int(r["current_step"])

        rows = c.execute(
            """SELECT id FROM approval_steps
               WHERE approval_id=? AND status='pending' AND step_order > ?
               ORDER BY step_order""",
            (approval_id, cur_step),
        ).fetchall()
        allowed_ids = [rid["id"] for rid in rows]
        if sorted(new_order_ids) != sorted(allowed_ids):
            return JSONResponse({"ok": False, "error": "invalid_ids"}, status_code=400)

        try:
            c.execute("BEGIN")
            # 1) 搬離
            c.executemany(
                "UPDATE approval_steps SET step_order = step_order + ? WHERE id=? AND approval_id=?",
                [(SHIFT, sid, approval_id) for sid in allowed_ids],
            )
            # 2) 寫回
            next_order = cur_step + 1
            for sid in new_order_ids:
                c.execute(
                    "UPDATE approval_steps SET step_order=? WHERE id=? AND approval_id=?",
                    (next_order, sid, approval_id),
                )
                next_order += 1

            c.execute(
                """INSERT INTO approval_actions(approval_id, step_id, actor, action, note, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (
                    approval_id,
                    None,
                    user,
                    "reorder_steps",
                    "調整未簽關卡順序",
                    now_iso(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            _renumber_steps(approval_id)
            return JSONResponse(
                {"ok": False, "error": "integrity_conflict"}, status_code=409
            )

    _renumber_steps(approval_id)
    return JSONResponse({"ok": True})


# ========= Debug =========
@router.get("/debug/ping")
async def debug_ping(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse({"error": "admin only"}, status_code=403)

    info: Dict[str, Any] = {}
    try:
        db_file = Path(DB_PATH)
        info["db_path"] = str(db_file)
        info["db_exists"] = db_file.exists()
        info["db_size_bytes"] = db_file.stat().st_size if info["db_exists"] else 0
        info["db_mtime"] = (
            datetime.fromtimestamp(db_file.stat().st_mtime).isoformat(
                timespec="seconds"
            )
            if info["db_exists"]
            else None
        )
        ensure_employees_table()
        with get_conn() as conn:
            c = conn.cursor()
            try:
                info["employees_count"] = c.execute(
                    "SELECT COUNT(*) AS c FROM employees"
                ).fetchone()["c"]
            except sqlite3.OperationalError as e:
                info["employees_table_error"] = str(e)
                info["employees_count"] = None
    except Exception as e:
        info["connection_error"] = str(e)

    return JSONResponse(info)


@router.get("/debug/dept")
async def debug_dept(
    dept: str = Query(..., description="部門名稱（與下拉相同）"),
    request: Request = None,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse({"error": "admin only"}, status_code=403)
    return {"dept": dept, "names": names_by_dept(dept)}


@router.get("/api/approvers/search")
async def api_search_approvers(
    request: Request,
    q: str = "",
    limit: int = 50,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return JSONResponse({"error": "permission_denied"}, status_code=403)

    ensure_employees_table()
    q = (q or "").strip()
    limit = max(1, min(int(limit or 50), 200))

    with get_conn() as conn:
        c = conn.cursor()
        if not q:
            rows = c.execute(
                """
                SELECT department, COALESCE(department_1,''), COALESCE(english_name,''), COALESCE(name,''), COALESCE(email,''), COALESCE(extension_number,'')
                FROM employees
                ORDER BY id DESC
                LIMIT ?
            """,
                (limit,),
            ).fetchall()
        else:
            kw = f"%{q}%"
            rows = c.execute(
                """
                SELECT department, COALESCE(department_1,''), COALESCE(english_name,''), COALESCE(name,''), COALESCE(email,''), COALESCE(extension_number,'')
                FROM employees
                WHERE department LIKE ? COLLATE NOCASE
                   OR department_1 LIKE ? COLLATE NOCASE
                   OR english_name LIKE ? COLLATE NOCASE
                   OR name LIKE ? COLLATE NOCASE
                   OR email LIKE ? COLLATE NOCASE
                   OR extension_number LIKE ? COLLATE NOCASE
                   OR team LIKE ? COLLATE NOCASE
                   OR job_title LIKE ? COLLATE NOCASE
                   OR employee_id LIKE ? COLLATE NOCASE
                ORDER BY department, department_1, english_name, name
                LIMIT ?
            """,
                (kw, kw, kw, kw, kw, kw, kw, kw, kw, limit),
            ).fetchall()

    results = []
    for r in rows:
        results.append(
            {
                "Department": r["department"] or "",
                "Department_1": r["department_1"] or "",
                "EnglishName": r["english_name"] or "",
                "Name": r["name"] or "",
                "Email": r["email"] or "",
                "Ext": r["extension_number"] or "",
            }
        )
    return results


@router.post("/debug/create_indexes")
async def debug_create_indexes(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse({"error": "admin only"}, status_code=403)

    ensure_employees_table()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_dept ON employees(department)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_dept1 ON employees(department_1)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_name ON employees(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_en ON employees(english_name)")
        conn.commit()
    return {"ok": True}


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]*?>", "", s or "")


@router.get("/api/search")
async def api_search(
    request: Request,
    q: str = "",
    scope: str = "all",  # all | todo | mine
    status: str = "all",  # all | pending | approved | rejected
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return JSONResponse({"error": "permission_denied"}, status_code=403)

    ensure_db()
    keys = user_identity_keys(user)
    limit = max(1, min(int(limit or 50), 200))
    q = (q or "").strip()

    base_sql = """
        SELECT a.id, a.subject, a.description, a.requester, a.status, a.current_step, a.submitted_at,
               s.approver_name AS cur_approver_name, s.status AS cur_step_status,
               a.view_scope
        FROM approvals a
        LEFT JOIN approval_steps s
          ON a.id = s.approval_id AND a.current_step = s.step_order
    """
    cond, params = [], []

    if status in ("pending", "approved", "rejected"):
        cond.append("a.status = ?")
        params.append(status)
    if date_from:
        cond.append("a.submitted_at >= ?")
        params.append(date_from)
    if date_to:
        cond.append("a.submitted_at <= ?")
        params.append(date_to)
    if q:
        like = f"%{q}%"
        cond.append(
            """(
                a.subject LIKE ? COLLATE NOCASE
             OR a.description LIKE ? COLLATE NOCASE
             OR a.requester LIKE ? COLLATE NOCASE
             OR EXISTS (
                    SELECT 1 FROM approval_steps s2
                    WHERE s2.approval_id = a.id
                      AND (s2.approver_name LIKE ? COLLATE NOCASE
                           OR s2.approver_email LIKE ? COLLATE NOCASE)
                )
            )"""
        )
        params += [like, like, like, like, like]
    if scope == "mine":
        cond.append("a.requester = ?")
        params.append(user)

    where = (" WHERE " + " AND ".join(cond)) if cond else ""
    sql = base_sql + where + " ORDER BY a.submitted_at DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        rid = r["id"]
        subject = r["subject"] or ""
        desc = r["description"] or ""
        requester = r["requester"] or ""
        a_status = r["status"]
        cur_step = r["current_step"]
        submitted_at = _format_taipei(r["submitted_at"])
        cur_name = r["cur_approver_name"]
        cur_step_status = r["cur_step_status"]
        scope_value = _normalize_scope(r["view_scope"])

        # scope=todo：僅列本人待簽
        if scope == "todo":
            if not (
                a_status == "pending"
                and cur_step_status == "pending"
                and _normi(cur_name) in keys
            ):
                continue

        try:
            can_view = can_view_approval(rid, user, role, permissions)
        except Exception:
            can_view = False

        if scope_value == "top_secret" and not can_view:
            # 極機密：未被授權者看不到搜尋結果
            continue

        preview = _strip_html(desc)[:120]
        step_disp = (cur_step + 1) if (cur_step is not None and cur_step >= 0) else None

        results.append(
            {
                "id": rid,
                "subject": subject,
                "requester": requester,
                "status": a_status,
                "current_step": step_disp,
                "submitted_at": submitted_at,
                "preview": preview,
                "can_view": bool(can_view),  # <=== 給前端用
            }
        )

    return results


@router.get("/{approval_id}/file/{file_id}")
async def download_file(
    approval_id: int,
    file_id: int,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return JSONResponse({"error": "permission_denied"}, status_code=403)

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
            SELECT orig_name, stored_name, content_type
            FROM approval_files
            WHERE id=? AND approval_id=?
        """,
            (file_id, approval_id),
        )
        row = c.fetchone()
        if not row:
            return HTMLResponse("File not found", status_code=404)
        orig, stored, ctype = row["orig_name"], row["stored_name"], row["content_type"]

    path = UPLOAD_ROOT / str(approval_id) / stored
    if not path.exists():
        return HTMLResponse("File missing on disk", status_code=404)

    return FileResponse(
        str(path),
        media_type=(ctype or "application/octet-stream"),
        filename=orig,
    )


@router.post("/debug/build_step_index")
async def debug_build_step_index(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse({"ok": False, "error": "admin only"}, status_code=403)

    ensure_db()
    _renumber_all_steps()
    created = False
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_steps_order
                ON approval_steps(approval_id, step_order)
            """
            )
            conn.commit()
            created = True
    except sqlite3.IntegrityError:
        _renumber_all_steps()
        try:
            with get_conn() as conn:
                c = conn.cursor()
                c.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_steps_order
                    ON approval_steps(approval_id, step_order)
                """
                )
                conn.commit()
                created = True
        except sqlite3.IntegrityError:
            pass

    return JSONResponse(
        {
            "ok": True,
            "msg": (
                "步驟序號已重整，索引已建立/確認存在。"
                if created
                else "步驟序號已重整（索引原本就存在）。"
            ),
        }
    )


@router.post("/debug/build_employee_indexes")
async def debug_build_employee_indexes(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse({"ok": False, "error": "admin only"}, status_code=403)

    ensure_employees_table()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_dept ON employees(department)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_dept1 ON employees(department_1)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_name ON employees(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_en ON employees(english_name)")
        conn.commit()
    return {"ok": True, "msg": "員工索引建立完成"}


# ========= 刪除簽核關卡（僅允許 pending 且在目前關卡之後） =========
# routers/approvals_router.py
# ... (rest of the file remains the same)


# ========= 刪除簽核關卡（僅允許 pending 且在目前關卡之後） =========
@router.delete("/{approval_id}/steps/{step_id}/delete", status_code=200)
@router.post("/{approval_id}/steps/{step_id}/delete")
async def delete_approver_step(
    approval_id: int,
    step_id: int,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    logging.info(
        f"== Starting DELETE request for step {step_id} in approval {approval_id} =="
    )

    if not user:
        logging.warning("User not authenticated.")
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        logging.warning(f"Permission denied for user {user}.")
        return JSONResponse(
            {"ok": False, "error": "permission_denied"}, status_code=403
        )
    if not _is_requester_or_admin(approval_id, user, role):
        logging.warning(f"User {user} is not requester or admin.")
        return JSONResponse(
            {"ok": False, "error": "only_requester_or_admin"}, status_code=403
        )

    ensure_db()
    try:
        with get_conn() as conn:
            c = conn.cursor()

            # Step 1: Check approval status
            logging.info("Checking approval status...")
            r = c.execute(
                "SELECT status, current_step FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if not r:
                logging.error(f"Approval {approval_id} not found. Returning 404.")
                return JSONResponse(
                    {"ok": False, "error": "not_found"}, status_code=404
                )
            if r["status"] != "pending":
                logging.warning(
                    f"Approval {approval_id} is not pending. Status: {r['status']}. Returning 400."
                )
                return JSONResponse(
                    {"ok": False, "error": "approval_not_pending"}, status_code=400
                )
            cur_step = int(r["current_step"])

            # Step 2: Check if the step exists and can be deleted
            logging.info(f"Checking step {step_id} status...")
            s = c.execute(
                """SELECT id, step_order, status, approver_name
                             FROM approval_steps WHERE id=? AND approval_id=?""",
                (step_id, approval_id),
            ).fetchone()
            if not s:
                logging.error(
                    f"Step {step_id} not found for approval {approval_id}. Returning 404."
                )
                return JSONResponse(
                    {"ok": False, "error": "step_not_found"}, status_code=404
                )
            if s["status"] != "pending" or int(s["step_order"]) <= cur_step:
                logging.warning(
                    f"Cannot delete step {step_id} (status: {s['status']}, order: {s['step_order']}). Returning 400."
                )
                return JSONResponse(
                    {"ok": False, "error": "cannot_delete_this_step"}, status_code=400
                )

            # Step 3: Execute deletion by temporarily disabling foreign key checks
            logging.info("Disabling foreign key checks to prevent integrity errors...")
            c.execute("PRAGMA foreign_keys = OFF")

            logging.info("Starting database transaction...")
            conn.execute("BEGIN TRANSACTION")

            # 3a: Delete related records from approval_actions
            logging.info(
                f"Deleting related records from approval_actions for step {step_id}..."
            )
            c.execute(
                """DELETE FROM approval_actions WHERE approval_id = ? AND step_id = ?""",
                (approval_id, step_id),
            )

            # 3b: Delete the record from approval_steps
            logging.info(f"Deleting record from approval_steps for id {step_id}...")
            c.execute("DELETE FROM approval_steps WHERE id=?", (step_id,))

            # 3c: Insert the new action log
            logging.info("Inserting new action log...")
            c.execute(
                """INSERT INTO approval_actions(approval_id, step_id, actor, action, note, created_at)
                             VALUES(?,?,?,?,?,?)""",
                (
                    approval_id,
                    step_id,
                    user,
                    "delete_step",
                    f"刪除簽核人：{s['approver_name']}",
                    now_iso(),
                ),
            )

            conn.commit()
            logging.info("Transaction committed successfully. Deletion complete.")

    except Exception as e:
        conn.rollback()
        logging.error(
            f"An unexpected error occurred during delete: {e}. Rolling back transaction."
        )
        return JSONResponse({"ok": False, "error": "database_error"}, status_code=500)
    finally:
        # Step 4: Always re-enable foreign key checks
        logging.info("Re-enabling foreign key checks.")
        with get_conn() as final_conn:
            final_conn.execute("PRAGMA foreign_keys = ON")
            final_conn.commit()

    _renumber_steps(approval_id)
    logging.info(
        f"Step order renumbered for approval {approval_id}. Request finished successfully."
    )
    return JSONResponse({"ok": True})
