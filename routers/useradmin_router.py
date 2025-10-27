# routers/useradmin_router.py
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse
import sqlite3
from typing import Optional

from app_utils import templates, get_base_context, get_conn, ensure_permission_pack

PREFIX = "useradmin"
router = APIRouter(prefix=f"/{PREFIX}", tags=["User Admin"])

# ① 權限鍵自動建立：useradmin_view / useradmin_manage / useradmin_admin
ensure_permission_pack(PREFIX)


# --- 安全解析目前登入者資訊，兼容不同專案寫法 ---
def _resolve_principal(request):
    """
    回傳 (user, role, permissions_raw)
    依序嘗試：
    1) app_utils.get_session_principal(request) -> (user, role, permissions_raw)
    2) app_utils.get_principal(request) -> (user, role, permissions_raw)
    3) request.session['user'/'role'/'permissions_raw']
    4) request.state.user / role / permissions_raw
    5) 預設 (None, 'guest', [])
    """
    # 1) & 2) 嘗試專案現有工具
    try:
        from app_utils import get_session_principal  # type: ignore

        return get_session_principal(request)
    except Exception:
        pass
    try:
        from app_utils import get_principal  # type: ignore

        return get_principal(request)
    except Exception:
        pass

    # 3) SessionMiddleware 典型寫法
    try:
        sess = getattr(request, "session", None) or {}
        u = sess.get("user")
        r = sess.get("role", "guest")
        p = sess.get("permissions_raw", [])
        if u is not None or p or r != "guest":
            return (u, r, p)
    except Exception:
        pass

    # 4) request.state
    try:
        st = getattr(request, "state", None)
        if st:
            u = getattr(st, "user", None)
            r = getattr(st, "role", "guest")
            p = getattr(st, "permissions_raw", [])
            return (u, r, p)
    except Exception:
        pass

    # 5) fallback
    return (None, "guest", [])


def _dep_require(perm: str):
    try:
        from app_utils import require_permission  # 你現有的依賴

        return Depends(require_permission(perm))
    except Exception:

        def _noop():
            return True

        return Depends(_noop)


def _hash_password_if_available(raw: str) -> str:
    try:
        from app_utils import hash_password

        return hash_password(raw)
    except Exception:
        return raw  # 維持相容（若你尚未導入雜湊）


def upsert_employee(
    folder: Optional[str],
    cost_center: Optional[str],
    department: Optional[str],
    department_1: Optional[str],
    team: Optional[str],
    job_title: Optional[str],
    name: str,
    employee_id: str,
    english_name: Optional[str],
    email: Optional[str],
    extension_number: Optional[str],
):
    sql = """
    INSERT INTO employees
      (folder, cost_center, department, department_1, team, job_title, name,
       employee_id, english_name, email, extension_number)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(employee_id) DO UPDATE SET
      folder=excluded.folder,
      cost_center=excluded.cost_center,
      department=excluded.department,
      department_1=excluded.department_1,
      team=excluded.team,
      job_title=excluded.job_title,
      name=excluded.name,
      english_name=excluded.english_name,
      email=excluded.email,
      extension_number=excluded.extension_number;
    """
    with get_conn("data/approvals.db") as conn:
        conn.execute(
            sql,
            (
                folder,
                cost_center,
                department,
                department_1,
                team,
                job_title,
                name,
                employee_id,
                english_name,
                email,
                extension_number,
            ),
        )
        conn.commit()


def upsert_user(
    loginname: str,
    password: str,
    display_name: str,
    role: str = "guest",
    status: int = 1,
):
    hashed = _hash_password_if_available(password)
    sql = """
    INSERT INTO users (loginname, password, display_name, role, status, updated_at)
    VALUES (?,?,?,?,?, CURRENT_TIMESTAMP)
    ON CONFLICT(loginname) DO UPDATE SET
      password=excluded.password,
      display_name=excluded.display_name,
      role=excluded.role,
      status=excluded.status,
      updated_at=CURRENT_TIMESTAMP;
    """
    with get_conn("data/auth.db") as conn:
        conn.execute(sql, (loginname, hashed, display_name, role, status))
        conn.commit()


@router.get("/", dependencies=[_dep_require(f"{PREFIX}_view")])
async def form_page(
    request: Request, ok: Optional[int] = None, err: Optional[str] = None
):
    user, role, permissions_raw = _resolve_principal(request)
    ctx = get_base_context(request, user, role, permissions_raw)
    ctx.update(
        {
            "active_menu": "useradmin",
            "active_key": "useradmin",
            "title": "使用者同步",
            "ok": ok,
            "err": err,
        }
    )
    return templates.TemplateResponse("useradmin/useradmin_form.html", ctx)


def _ctx(request: Request, active: str):
    base = get_base_context(request)
    base.update({"active_menu": active, "active_key": active})
    return base


@router.post("/save", dependencies=[_dep_require(f"{PREFIX}_manage")])
async def save_user(
    request: Request,
    # employees
    folder: Optional[str] = Form(None),
    cost_center: Optional[str] = Form(None),
    department: Optional[str] = Form(None),
    department_1: Optional[str] = Form(None),
    team: Optional[str] = Form(None),
    job_title: Optional[str] = Form(None),
    name: str = Form(...),
    employee_id: str = Form(...),
    english_name: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    extension_number: Optional[str] = Form(None),
    # users
    loginname: Optional[str] = Form(None),
    password: str = Form(...),
    role: str = Form("guest"),
    status: int = Form(1),
):
    try:
        # 1) approvals.db → employees
        upsert_employee(
            folder,
            cost_center,
            department,
            department_1,
            team,
            job_title,
            name,
            employee_id,
            english_name,
            email,
            extension_number,
        )
        # 2) auth.db → users
        _login = loginname or employee_id
        upsert_user(_login, password, display_name=name, role=role, status=status)
        return RedirectResponse(url=f"/{PREFIX}?ok=1", status_code=303)
    except sqlite3.IntegrityError as e:
        return RedirectResponse(url=f"/{PREFIX}?err={str(e)}", status_code=303)
    except Exception as e:
        return RedirectResponse(
            url=f"/{PREFIX}?err={type(e).__name__}: {str(e)}", status_code=303
        )
