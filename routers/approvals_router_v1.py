# routers/approvals_router.py
from fastapi import APIRouter, Request, Cookie, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from typing import Optional, List, Tuple, Set
import sqlite3
import os
from datetime import datetime, timezone
import smtplib
from email.message import EmailMessage
import json
from pathlib import Path

from app_utils import get_base_context, templates

# ========= 路徑與 DB 連線 =========
# 以專案根為基準 <root>/data/approvals.db；可用環境變數 APPROVALS_DB 覆蓋
BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = BASE_DIR / "data" / "approvals.db"
DB_PATH = os.getenv("APPROVALS_DB", str(DEFAULT_DB))

router = APIRouter(prefix="/approvals", tags=["Approvals"])


def get_conn():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


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
    """確保簽核相關三表存在"""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approvals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            description TEXT,
            requester TEXT NOT NULL,
            requester_dept TEXT,
            submitted_at TEXT NOT NULL,
            status TEXT NOT NULL,
            current_step INTEGER NOT NULL
        )
        """
        )
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS approval_steps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            approval_id INTEGER NOT NULL,
            step_order INTEGER NOT NULL,
            approver_name TEXT NOT NULL,   -- 顯示用：通常中文姓名
            approver_email TEXT,
            status TEXT NOT NULL,
            decided_at TEXT,
            comment TEXT,
            FOREIGN KEY(approval_id) REFERENCES approvals(id)
        )
        """
        )
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
            FOREIGN KEY(approval_id) REFERENCES approvals(id),
            FOREIGN KEY(step_id) REFERENCES approval_steps(id)
        )
        """
        )
        conn.commit()


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
    """用中文名 / 英文名 / 英+中 組合找 email"""
    found = _find_employee_by_display(any_name)
    if not found:
        return None
    _, _, mail = found
    return mail if ("@" in (mail or "")) else None


def user_identity_keys(login_user: str) -> Set[str]:
    """
    產生此使用者可被接受的所有身分鍵：
    - 登入帳號本身
    - 若能在 employees 找到對應列，加入：英文名 / 中文名 / 英+中 / email local-part
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
        local = (mail.split("@")[0] if mail else "")
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
        return JSONResponse({"ok": False, "error": "請至少選擇一位串簽人員"}, status_code=400)

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

        # 建立 steps：以 DB 對照到的人為準（優先中文存 approver_name）
        for idx, disp in enumerate(chain):
            emp = _find_employee_by_display(disp)
            if emp:
                en, nm, mail = emp
                save_name = (nm or en or disp)
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
                    dict(id=r[0], subject=r[1], requester=r[2], submitted_at=r[3], step=r[4])
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
            dict(id=r[0], subject=r[1], status=r[2], submitted_at=r[3], current_step=r[4])
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
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, subject, description, requester, requester_dept, status, current_step, submitted_at FROM approvals WHERE id=?",
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

        c.execute(
            "SELECT id, step_order, approver_name, approver_email, status, decided_at, comment FROM approval_steps WHERE approval_id=? ORDER BY step_order",
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

        c.execute(
            "SELECT actor, action, note, created_at FROM approval_actions WHERE approval_id=? ORDER BY id",
            (approval_id,),
        )
        actions = [
            dict(actor=r[0], action=r[1], note=r[2], created_at=r[3])
            for r in c.fetchall()
        ]

    # 能否簽核（你是目前關卡 + 該關 pending）
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
        # 友善：帶錯誤碼回詳情頁
        return RedirectResponse(
            url=f"/approvals/{approval_id}?error=comment_required",
            status_code=303
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

        # 寫入歷程
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
            datetime.fromtimestamp(db_file.stat().st_mtime).isoformat(timespec="seconds")
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

            try:
                rows = c.execute(
                    """
                    SELECT DISTINCT TRIM(x) AS d
                    FROM (
                        SELECT department AS x FROM employees
                        UNION ALL
                        SELECT department_1 AS x FROM employees
                    )
                    WHERE TRIM(COALESCE(x,'')) <> ''
                    ORDER BY d
                    LIMIT 20
                """
                ).fetchall()
                depts = [r[0] for r in rows]
                info["sample_departments"] = depts
                info["departments_count_sampled"] = len(depts)

                if depts:
                    people = c.execute(
                        """
                        SELECT english_name, name
                        FROM employees
                        WHERE TRIM(COALESCE(department,'')) = TRIM(?)
                           OR TRIM(COALESCE(department_1,'')) = TRIM(?)
                        LIMIT 20
                    """,
                        (depts[0], depts[0]),
                    ).fetchall()
                    info["sample_people_for_first_dept"] = [
                        _combine_label(en, nm) for en, nm in people
                    ]
            except sqlite3.OperationalError as e:
                info["departments_error"] = str(e)
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
    # 權限檢查（需登入 + approvals 權限或 admin）
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
            # 最近（以 id DESC 當作近似）
            rows = c.execute("""
                SELECT department, COALESCE(department_1,''), COALESCE(english_name,''), COALESCE(name,''), COALESCE(email,''), COALESCE(extension_number,'')
                FROM employees
                ORDER BY id DESC
                LIMIT ?
            """, (limit,)).fetchall()
        else:
            kw = f"%{q}%"
            rows = c.execute("""
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
            """, (kw,kw,kw,kw,kw,kw,kw,kw,kw,limit)).fetchall()

    # 對齊前端 HEADERS
    results = []
    for d, d1, en, nm, mail, ext in rows:
        results.append({
            "Department": d or "",
            "Department_1": d1 or "",
            "EnglishName": en or "",
            "Name": nm or "",
            "Email": mail or "",
            "Ext": ext or "",
        })
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
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_emp_en ON employees(english_name)"
        )
        conn.commit()
    return {"ok": True}
