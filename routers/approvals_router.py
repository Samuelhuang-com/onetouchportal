# routers/approvals_router.py
from fastapi import APIRouter, Request, Cookie, Form, Query, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from typing import Optional, List, Tuple, Set
import sqlite3
import os
from datetime import datetime, timezone
import smtplib
from email.message import EmailMessage
import json
from pathlib import Path
from uuid import uuid4
import re

from app_utils import get_base_context, templates

# ========= 路徑與 DB 連線 =========
# 以專案根為基準 <root>/data/approvals.db；可用環境變數 APPROVALS_DB 覆蓋
BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE_DIR / "data" / "approvals.db"
DB_PATH = os.getenv("APPROVALS_DB", str(DEFAULT_DB))

router = APIRouter(prefix="/approvals", tags=["Approvals"])

UPLOAD_ROOT = BASE_DIR / "data" / "uploads" / "approvals"


def _safe_filename(name: str) -> str:
    """拿掉路徑、僅保留安全字元"""
    base = Path(str(name or "file")).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ========= 小工具 =========
def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _combine_label(english_name: str, name: str) -> str:
    a = str(english_name or "").strip()
    b = str(name or "").strip()
    if a and b:
        return f"{a} {b}"
    return a or b


def _norm(s: str) -> str:
    return " ".join(str(s or "").split())


def _normi(s: str) -> str:
    """大小寫不敏感 + 空白壓縮"""
    return _norm(s).casefold()

# ====== 修復工具：重新編號 step_order，避免舊資料重覆 ======
def _renumber_steps(approval_id: int):
    with get_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id FROM approval_steps WHERE approval_id=? ORDER BY step_order, id",
            (approval_id,)
        ).fetchall()
        for idx, (sid,) in enumerate(rows):
            c.execute("UPDATE approval_steps SET step_order=? WHERE id=?", (idx, sid))
        conn.commit()

def _renumber_all_steps():
    with get_conn() as conn:
        c = conn.cursor()
        ids = c.execute("SELECT DISTINCT approval_id FROM approval_steps").fetchall()
    for (aid,) in ids:
        _renumber_steps(aid)

# ========= 建表（若不存在）=========
def ensure_employees_table():
    """確保 employees 表存在（不覆蓋既有資料）"""
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
    """確保簽核相關表存在；建立前先自癒舊資料，避免索引建立失敗"""
    with get_conn() as conn:
        c = conn.cursor()
        # 主表
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            description TEXT,
            requester TEXT NOT NULL,
            requester_dept TEXT,
            submitted_at TEXT NOT NULL,
            status TEXT NOT NULL,          -- pending / approved / rejected
            current_step INTEGER NOT NULL  -- 0-based；完成/退回為 -1
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
            FOREIGN KEY(step_id) REFERENCES approval_steps(id) ON DELETE SET NULL
        )
        """
        )
        # 附件（若你已經有就保留）
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
        conn.commit()

    # 先做一次全量自癒，處理舊資料的重覆序號
    try:
        _renumber_all_steps()
    except Exception as e:
        print("[approvals] renumber all steps failed:", e)

    # 建立唯一索引；若因殘留資料又失敗，重跑自癒一次並略過
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
        # 極少數情況：前一輪資料又變動造成碰撞，再自癒一次
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
            # 若仍失敗，就先不建立索引（不影響功能），並輸出 log
            print("[approvals] WARN: create unique index failed after renumber:", e)


# ========= 從 employees 取資料 =========
def list_departments() -> List[str]:
    """從 employees 的 department / department_1 擷取去重後的部門清單"""
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
        return [r[0] for r in rows]
    except Exception as e:
        print("[approvals] list_departments error:", e)
        return []


def names_by_dept(dept: str) -> List[str]:
    """依部門取人員，顯示為 english_name + name（任一缺省就用另一個）"""
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
    for en, nm in rows:
        label = _combine_label(en, nm)
        if label and label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def _find_employee_by_display(display_name: str) -> Optional[Tuple[str, str, str]]:
    """由顯示名稱（英文/中文/英+中）找出 (english_name, name, email)"""
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

    for en, nm, mail in rows:
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
    """
    產生此使用者可被接受的所有身分鍵：登入帳號 / 英文名 / 中文名 / 英+中 / email local-part
    """
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

    for en, nm, mail in rows:
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
        return bool(r and r[0] == user)


def _renumber_steps(approval_id: int):
    """把此單所有關卡重新編號為 0..n-1（依 step_order,id 排序），避免序號重覆"""
    with get_conn() as conn:
        c = conn.cursor()
        rows = c.execute(
            "SELECT id FROM approval_steps WHERE approval_id=? ORDER BY step_order, id",
            (approval_id,),
        ).fetchall()
        for idx, (sid,) in enumerate(rows):
            c.execute("UPDATE approval_steps SET step_order=? WHERE id=?", (idx, sid))
        conn.commit()


def _renumber_all_steps():
    """針對所有單據做一次序號重整；用於建立唯一索引前的『自癒』"""
    with get_conn() as conn:
        c = conn.cursor()
        ids = c.execute("SELECT DISTINCT approval_id FROM approval_steps").fetchall()
    for (aid,) in ids:
        _renumber_steps(aid)


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
    approver_chain: str = Form(...),  # JSON：["Samuel Huang 黃金昇", "..."]
    files: List[UploadFile] = File([]),  # 多檔附件
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
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

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
        INSERT INTO approvals(subject, description, requester, requester_dept, submitted_at, status, current_step)
        VALUES(?,?,?,?,?,?,?)
        """,
            (subject, description, requester, requester_dept, now_iso(), "pending", 0),
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
                save_name = str(disp).strip()
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
                # 分塊寫入
                while True:
                    chunk = await uf.read(1024 * 1024)  # 1MB
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
            name, mail = row
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
            appr_name = r[5]
            if _normi(appr_name) in keys:
                my_todo.append(
                    dict(
                        id=r[0],
                        subject=r[1],
                        requester=r[2],
                        submitted_at=r[3],
                        step=r[4],
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
                id=r[0], subject=r[1], status=r[2], submitted_at=r[3], current_step=r[4]
            )
            for r in c.fetchall()
        ]

    ctx = get_base_context(request, user, role, permissions)
    ctx["my_todo"] = my_todo
    ctx["my_sent"] = my_sent
    # ctx["attachments"] = attachments
    ctx["role"] = role
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
    _renumber_steps(approval_id)  # ★ 自動整理序號，避免舊資料重覆

    with get_conn() as conn:
        c = conn.cursor()

        # 主檔
        c.execute(
            """
            SELECT id, subject, description, requester, requester_dept,
                   status, current_step, submitted_at
            FROM approvals WHERE id=?
        """,
            (approval_id,),
        )
        a = c.fetchone()
        if not a:
            return HTMLResponse("Not found", status_code=404)
        approval = dict(
            id=a[0],
            subject=a[1],
            description=a[2],
            requester=a[3],
            requester_dept=a[4],
            status=a[5],
            current_step=a[6],
            submitted_at=a[7],
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
                id=r[0],
                step_order=r[1],
                approver_name=r[2],
                approver_email=r[3],
                status=r[4],
                decided_at=r[5],
                comment=r[6],
            )
            for r in c.fetchall()
        ]

        # 歷程
        c.execute(
            """
            SELECT actor, action, note, created_at
            FROM approval_actions
            WHERE approval_id=? ORDER BY id
        """,
            (approval_id,),
        )
        actions = [
            dict(actor=r[0], action=r[1], note=r[2], created_at=r[3])
            for r in c.fetchall()
        ]

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
            dict(id=r[0], name=r[1], ctype=r[2], size=r[3], by=r[4], at=r[5])
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

    ctx = get_base_context(request, user, role, permissions)
    ctx["approval"] = approval
    ctx["steps"] = steps
    ctx["actions"] = actions
    ctx["can_act"] = can_act
    ctx["current_step_obj"] = cur_obj
    ctx["attachments"] = attachments
    # ★ 詳情頁「新增串簽人員」需要的資料/權限
    ctx["departments"] = list_departments()
    ctx["can_manage_chain"] = _is_requester_or_admin(approval_id, user, role) and (
        approval["status"] == "pending"
    )
    return templates.TemplateResponse("approvals/detail.html", ctx)


# ========= 簽核動作（同意 / 退回） =========
@router.post("/{approval_id}/action")
async def do_action(
    approval_id: int,
    action: str = Form(...),  # approve / reject
    comment: str = Form(...),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not can_use_approvals(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ensure_db()
    # 後端強制：簽核意見必填（去空白）
    if not (comment or "").strip():
        return RedirectResponse(
            url=f"/approvals/{approval_id}?error=comment_required", status_code=303
        )
    comment = comment.strip()

    with get_conn() as conn:
        c = conn.cursor()
        # 目前 step
        c.execute(
            "SELECT current_step, subject, description, requester FROM approvals WHERE id=?",
            (approval_id,),
        )
        row = c.fetchone()
        if not row:
            return HTMLResponse("Not found", status_code=404)
        cur_step, subject, desc, requester = row

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
        step_id, approver_name, approver_email = s

        # 身分授權（多身分鍵）
        keys = user_identity_keys(user)
        if _normi(approver_name) not in keys:
            return HTMLResponse("您不是此關簽核人", status_code=403)

        # 更新步驟狀態
        new_status = "approved" if action == "approve" else "rejected"
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

        # 同意 → 前進或完成
        c.execute(
            """SELECT COUNT(*) FROM approval_steps
                     WHERE approval_id=?""",
            (approval_id,),
        )
        total_steps = c.fetchone()[0]

        next_step = cur_step + 1
        if next_step >= total_steps:
            # 全部完成
            c.execute(
                "UPDATE approvals SET status=?, current_step=? WHERE id=?",
                ("approved", -1, approval_id),
            )
            conn.commit()
            # 通知申請人完成
            send_mail_safe(
                [email_by_name(requester)] if email_by_name(requester) else [],
                f"【簽核完成】{subject}",
                f"{requester} 您好：\n\n主旨：{subject}\n所有關卡已完成簽核。\n\n請登入系統查看。",
            )
        else:
            # 前進下一關
            c.execute(
                "UPDATE approvals SET current_step=? WHERE id=?",
                (next_step, approval_id),
            )
            conn.commit()
            # 通知下一關
            c.execute(
                """SELECT approver_name, approver_email FROM approval_steps
                         WHERE approval_id=? AND step_order=?""",
                (approval_id, next_step),
            )
            nx = c.fetchone()
            if nx:
                nx_name, nx_mail = nx
                send_mail_safe(
                    [nx_mail] if nx_mail else [],
                    f"【簽核通知】請審核：{subject}",
                    f"{nx_name} 您好：\n\n有一筆文件等待您審核。\n主旨：{subject}\n說明：{desc}\n送簽人：{requester}\n\n請進入系統 /approvals 查看並簽核。",
                )

    return RedirectResponse(url=f"/approvals/{approval_id}", status_code=303)


# ========= 新增簽核人員（加入至最後一關，避免重覆名） =========
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
        if r[0] != "pending":
            return JSONResponse(
                {"ok": False, "error": "approval_not_pending"}, status_code=400
            )

        # 去重：同名不可再加入（大小寫/空白不敏感）
        rows = c.execute(
            "SELECT approver_name FROM approval_steps WHERE approval_id=?",
            (approval_id,),
        ).fetchall()
        names_n = {_normi(x[0]) for x in rows if x and x[0]}
        if _normi(save_name) in names_n:
            return JSONResponse(
                {"ok": False, "error": "duplicated_name"}, status_code=400
            )

        # 取最後一關序號
        r = c.execute(
            "SELECT MAX(step_order) FROM approval_steps WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        next_order = (int(r[0]) + 1) if (r and r[0] is not None) else 0

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


# ========= 重新排序（僅允許調整「目前關卡之後」且「未簽」的關卡） =========
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

    with get_conn() as conn:
        c = conn.cursor()
        r = c.execute(
            "SELECT status, current_step FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if not r:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        if r[0] != "pending":
            return JSONResponse(
                {"ok": False, "error": "approval_not_pending"}, status_code=400
            )
        cur_step = int(r[1])

        rows = c.execute(
            """SELECT id FROM approval_steps
               WHERE approval_id=? AND status='pending' AND step_order > ?
               ORDER BY step_order""",
            (approval_id, cur_step),
        ).fetchall()
        allowed_ids = [rid for (rid,) in rows]
        if sorted(new_order_ids) != sorted(allowed_ids):
            return JSONResponse({"ok": False, "error": "invalid_ids"}, status_code=400)

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
            (approval_id, None, user, "reorder_steps", "調整未簽關卡順序", now_iso()),
        )
        conn.commit()

    _renumber_steps(approval_id)
    return JSONResponse({"ok": True})


# ========= Debug（admin） =========
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

    info = {}
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
                    "SELECT COUNT(*) FROM employees"
                ).fetchone()[0]
            except sqlite3.OperationalError as e:
                info["employees_table_error"] = str(e)
                info["employees_count"] = None
    except Exception as e:
        info["connection_error"] = str(e)

    return JSONResponse(info)

@router.post("/debug/build_step_index")
async def debug_build_step_index(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse({"ok": False, "error": "admin only"}, status_code=403)

    # 先修復舊資料 → 再建立唯一索引
    ensure_db()
    _renumber_all_steps()
    created = False
    try:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_steps_order
                ON approval_steps(approval_id, step_order)
            """)
            conn.commit()
            created = True
    except sqlite3.IntegrityError:
        # 若仍有殘留，重跑修復再試一次；若再失敗就放過（功能不受影響）
        _renumber_all_steps()
        try:
            with get_conn() as conn:
                c = conn.cursor()
                c.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_steps_order
                    ON approval_steps(approval_id, step_order)
                """)
                conn.commit()
                created = True
        except sqlite3.IntegrityError:
            pass

    return JSONResponse({
        "ok": True,
        "msg": "步驟序號已重整，索引已建立/確認存在。" if created else "步驟序號已重整（索引原本就存在）。"
    })

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
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_dept   ON employees(department)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_dept1  ON employees(department_1)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_name   ON employees(name)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_emp_en     ON employees(english_name)")
        conn.commit()
    return {"ok": True, "msg": "員工索引建立完成"}

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
    for d, d1, en, nm, mail, ext in rows:
        results.append(
            {
                "Department": d or "",
                "Department_1": d1 or "",
                "EnglishName": en or "",
                "Name": nm or "",
                "Email": mail or "",
                "Ext": ext or "",
            }
        )
    return results


@router.post("/debug/create_indexes")
async def debug_create_indexes(
    request: Request,
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
               s.approver_name AS cur_approver_name, s.status AS cur_step_status
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
    for (
        rid,
        subject,
        desc,
        requester,
        a_status,
        cur_step,
        submitted_at,
        cur_name,
        cur_step_status,
    ) in rows:
        if scope == "todo":
            if not (
                a_status == "pending"
                and cur_step_status == "pending"
                and _normi(cur_name) in keys
            ):
                continue

        preview = _strip_html(desc)[:120]
        step_disp = (cur_step + 1) if (cur_step is not None and cur_step >= 0) else None
        results.append(
            {
                "id": rid,
                "subject": subject or "",
                "requester": requester or "",
                "status": a_status,
                "current_step": step_disp,
                "submitted_at": submitted_at or "",
                "preview": preview,
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
        orig, stored, ctype = row

    path = UPLOAD_ROOT / str(approval_id) / stored
    if not path.exists():
        return HTMLResponse("File missing on disk", status_code=404)

    return FileResponse(
        str(path),
        media_type=(ctype or "application/octet-stream"),
        filename=orig,
    )

@router.post("/debug/renumber_all")
async def debug_renumber_all(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse({"error": "admin only"}, status_code=403)
    _renumber_all_steps()
    return {"ok": True}
