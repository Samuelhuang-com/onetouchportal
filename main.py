from fastapi import (
    FastAPI,
    Request,
    Cookie,
    Depends,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
)
from fastapi.staticfiles import StaticFiles
from typing import Optional
import pandas as pd
from urllib.parse import quote
import os
import json

# 從 app_utils 匯入共用的函式和物件
from app_utils import get_base_context, templates, PERMISSION_FILE, ANNOUNCEMENTS_FILE

# ★ 核心修正：匯入新的儀表板工具函式
from dashboard_utils import get_dashboard_kpis
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

# 匯入各個功能的 router
from report_router import router as report_router
from contract_router import router as contract_router
from daily_log_router import router as daily_log_router
from auth_router import router as auth_router
from reservations_router import router as reservations_router
from announcement_router import router as announcement_router
from budget_router import router as budget_router
from user_router import router as user_router
from laundry_request_router import router as laundry_router
from laundry_report_router import router as laundry_report_router
from routers.rv_analysis_router import router as rv_router
from routers.msr02_view_router import router as msr02_view_router
from routers.callcenter_router import router as callcenter_router
from routers.approvals_router import router as approvals_router
from routers import debug_router  # 1. 匯入新的 debug_router
from permissions_router import router as permissions_router
from data.perm_service import has_permission
from roles_router import router as roles_router
from groups_router import router as groups_router
from data.db import init_db, seed_default_roles, upsert_permission_keys
from app_utils import NAV_ITEMS
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.status import HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR

# --- App 初始化 ---
app = FastAPI(title="飯店管理系統")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
# app.state.templates = templates

# --- Jinja2 filters: number / currency / percent ---
from decimal import Decimal, InvalidOperation

def _to_float(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(Decimal(str(x)))
    except (InvalidOperation, ValueError, TypeError):
        return None

def j2_number(value, digits=0):
    """格式化數字：3位一逗號，小數位數可調；None/無法轉換顯示 '-'"""
    v = _to_float(value)
    if v is None:
        return "-"
    return f"{v:,.{int(digits)}f}"

def j2_currency(value, symbol="NT$", digits=0):
    """貨幣：預設 NT$，千分位，小數位數可調"""
    v = _to_float(value)
    if v is None:
        return "-"
    return f"{symbol}{v:,.{int(digits)}f}"

def j2_percent(value, digits=1):
    """百分比：0.123 → 12.3%"""
    v = _to_float(value)
    if v is None:
        return "-"
    return f"{v*100:.{int(digits)}f}%"

templates.env.filters["number"] = j2_number
templates.env.filters["currency"] = j2_currency
templates.env.filters["percent"] = j2_percent
# --- end filters ---

# --- 全域未登入攔截 Middleware（新增） ---
from starlette.responses import JSONResponse


def render_error(request: Request, status_code: int, message: str = ""):
    # 可視需要決定首頁 URL
    ctx = {"request": request, "status_code": status_code, "message": message}
    return templates.TemplateResponse("error.html", ctx, status_code=status_code)

# 404：找不到頁面
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == HTTP_404_NOT_FOUND:
        return render_error(request, exc.status_code, "找不到這個頁面。")
    # 其它 HTTP 錯誤也用同一版型
    return render_error(request, exc.status_code, str(exc.detail))

# 500：未捕捉例外
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # 你可以在這裡加 log，或把 exc 轉成人看得懂的訊息
    return render_error(request, HTTP_500_INTERNAL_SERVER_ERROR, "系統發生未預期的錯誤。")

# 422：請求驗證錯誤（表單/Query 格式不對等）
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return render_error(request, 422, "送出的資料格式不正確，請檢查後再試。")

PUBLIC_PATHS = {
    "/",           # 你的登入頁(GET)在 / 這裡
    "/login",      # 允許未登入 POST /login
    "/logout",     # 登出不一定要驗證
    "/healthz",    # 若有健康檢查
}
PUBLIC_PREFIXES = (
    "/static",     # 靜態檔
    "/favicon",    # /favicon.ico
    "/robots.txt",
)

def _is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path

    # 未登入者允許的路徑
    if _is_public(path):
        return await call_next(request)

    # 其它一律要有 user cookie 才能進
    user = request.cookies.get("user")
    if not user:
        # 依需求：API/JSON 回 401，瀏覽器頁面轉回登入頁（/）
        accepts = request.headers.get("accept", "")
        if "application/json" in accepts or path.startswith("/api/"):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse(url="/", status_code=302)

    return await call_next(request)

# 掛載所有功能的 router
app.include_router(report_router)
app.include_router(contract_router)
app.include_router(auth_router)
app.include_router(daily_log_router)
app.include_router(reservations_router)
app.include_router(announcement_router)
app.include_router(budget_router)
app.include_router(user_router)
app.include_router(laundry_router)
app.include_router(laundry_report_router)
app.include_router(rv_router, tags=["Revenue"])
app.include_router(msr02_view_router)
app.include_router(callcenter_router)
app.include_router(approvals_router)
app.include_router(debug_router.router)  # 2. 註冊新的 router
app.include_router(permissions_router)
app.include_router(roles_router)
app.include_router(groups_router)

def check_permission(permission_key: str):
    async def _checker(
        user: Optional[str] = Cookie(None),
        role: Optional[str] = Cookie(None),
        permissions: Optional[str] = Cookie(None),
    ) -> bool:
        # 1) admin 直接通過
        if role == "admin":
            return True
        # 2) 優先用 cookie
        try:
            perms = json.loads(permissions) if permissions else {}
            if perms.get(permission_key, False):
                return True
        except Exception:
            pass
        # 3) 回 DB 再驗一次（避免被竄改/過期）
        return has_permission(user or "", permission_key)
    return _checker

# --- 權限檢查相依性 (可重複使用) ---
def check_permission(permission_key: str):
    """
    建立一個可重複使用的權限檢查函式 (Depends)。
    """

    async def _checker(
        role: Optional[str] = Cookie(None), permissions: Optional[str] = Cookie(None)
    ) -> bool:
        # 1. admin 直接擁有所有權限
        if role == "admin":
            return True
        # 2. 解析 permissions cookie
        try:
            perms_dict = json.loads(permissions) if permissions else {}
            # 3. 檢查對應的 key 是否為 True
            if perms_dict.get(permission_key, False):
                return True
        except (json.JSONDecodeError, TypeError):
            return False
        return False

    return _checker


@app.get("/", response_class=HTMLResponse)
async def home_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    # 如果你要登入後才看得到，可以加判斷
    if not user:
        return RedirectResponse(url="/")  # 或你的登入頁面路徑

    ctx = get_base_context(request, user, role, permissions)
    return templates.TemplateResponse("home.html", ctx)


# --- 核心路由 (已修正權限傳遞與保護) ---
# --- 請將此段程式碼加入到您的 main.py 中 ---


@app.get("/home", response_class=HTMLResponse)
async def home_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    """
    顯示新的功能總覽藍圖頁面 (home.html)。
    """
    if not user:
        return RedirectResponse(url="/")

    # 取得共用的 context
    ctx = get_base_context(request, user, role, permissions)

    # 渲染 home.html 頁面
    return templates.TemplateResponse("home.html", ctx)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")

    ctx = get_base_context(request, user, role, permissions)

    # --- ★ 核心修正：重新加入完整的數據獲取邏輯 ---
    # 1. 讀取公告
    latest_announcements = []
    can_view_announcements = (role == "admin") or ctx["permissions"].get(
        "view_announcements", False
    )
    if can_view_announcements:
        try:
            if os.path.exists(ANNOUNCEMENTS_FILE):
                with open(ANNOUNCEMENTS_FILE, "r", encoding="utf-8") as f:
                    all_announcements = json.load(f)
                latest_announcements = sorted(
                    all_announcements,
                    key=lambda x: x.get("timestamp", ""),
                    reverse=True,
                )[:5]
        except (FileNotFoundError, json.JSONDecodeError):
            latest_announcements = []

    # 2. 從 dashboard_utils 模組獲取儀表板 KPI
    dashboard_kpis = get_dashboard_kpis()

    # 3. 將所有數據加入到 context 中，確保 'kpis' 被傳遞
    ctx["announcements"] = latest_announcements
    ctx["kpis"] = dashboard_kpis

    return templates.TemplateResponse("dashboard.html", ctx)


@app.get("/employees", response_class=HTMLResponse)
async def employees_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_permission("employees")),
):
    if not user:
        return RedirectResponse(url="/")
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    records = []
    try:
        # 假設員工資料檔案名為 employees.xlsx
        records = pd.read_excel("data/employees.xlsx").to_dict(orient="records")
    except FileNotFoundError:
        # 如果檔案不存在，records 會是一個空列表，頁面會正常顯示「無資料」
        pass

    # 準備 context 並只傳入員工頁面需要的資料
    ctx = get_base_context(request, user, role, permissions)
    ctx["records"] = records

    # 確保渲染正確的 employees.html 樣板
    return templates.TemplateResponse("employees.html", ctx)


@app.get("/powerbi", response_class=HTMLResponse)
async def powerbi_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_permission("powerbi")),  # 加入權限檢查
):
    if not user:
        return RedirectResponse(url="/")
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")
    # ★ 修正：將 permissions 傳遞給 context
    ctx = get_base_context(request, user, role, permissions)
    return templates.TemplateResponse("powerbi.html", ctx)


@app.get("/hr_folder", response_class=HTMLResponse)
async def hr_folder_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_permission("hr_folder")),  # 加入權限檢查
):
    if not user:
        return RedirectResponse(url="/")
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    folder_path = "data/hr"
    files_to_render = []
    error_message = None
    try:
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            for item_name in os.listdir(folder_path):
                if os.path.isfile(os.path.join(folder_path, item_name)):
                    files_to_render.append(
                        {"name": item_name, "url_encoded_name": quote(item_name)}
                    )
        else:
            error_message = f"錯誤：伺服器找不到指定的路徑 '{folder_path}'。"
    except Exception as e:
        error_message = f"發生未預期的錯誤：{e}"

    # ★ 修正：將 permissions 傳遞給 context
    ctx = get_base_context(request, user, role, permissions)
    ctx["files"] = files_to_render
    ctx["error"] = error_message
    return templates.TemplateResponse("hrFolder.html", ctx)


@app.get("/hr_files/{file_path:path}")
async def serve_hr_file(file_path: str, user: Optional[str] = Cookie(None)):
    # 簡單保護，確保使用者已登入
    if not user:
        return HTMLResponse(content="Forbidden", status_code=403)

    base_dir = os.path.abspath("data/hr")
    requested_path = os.path.join(base_dir, file_path)
    if not os.path.abspath(requested_path).startswith(base_dir):
        return HTMLResponse(content="Forbidden", status_code=403)
    if os.path.exists(requested_path) and os.path.isfile(requested_path):
        return FileResponse(requested_path)
    return HTMLResponse(content="Not Found", status_code=404)


@app.get("/users", response_class=HTMLResponse)
async def manage_users_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_permission("users")),  # 加入權限檢查
):
    if not user:
        return RedirectResponse(url="/")
    # 這裡可以保留 role != 'admin' 的檢查，作為雙重保險
    if not has_permission or role != "admin":
        return RedirectResponse(url="/dashboard?error=permission_denied")

    try:
        users_df = pd.read_excel(PERMISSION_FILE, sheet_name="employeesfiles")
        users_list = users_df.to_dict("records")
    except Exception:
        users_list = []

    # ★ 修正：將 permissions 傳遞給 context
    ctx = get_base_context(request, user, role, permissions)
    ctx["users"] = users_list
    return templates.TemplateResponse("manage_users.html", ctx)


@app.get("/events", response_class=HTMLResponse)
async def events_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_permission("events")),  # 加入權限檢查
):
    if not user:
        return RedirectResponse(url="/")
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    events = []
    try:
        events = pd.read_csv("data/events.csv").to_dict(orient="records")
    except FileNotFoundError:
        pass
    # ★ 修正：將 permissions 傳遞給 context
    ctx = get_base_context(request, user, role, permissions)
    ctx["events"] = events
    return templates.TemplateResponse("events.html", ctx)


@app.get("")
def page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    ensure_workbook()
    ctx = get_base_context(request, user, role, permissions)
    if not (ctx.get("role") == "admin" or ctx.get("permissions", {}).get("callcenter")):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return templates.TemplateResponse("callcenter/callcenter.html", ctx)

# 啟動：建表 + 預設角色 + 同步權限鍵
@app.on_event("startup")
def _startup():
    init_db()
    seed_default_roles()
    # 同步 NAV 的 permission_keys（避免手動維護）
    keys = set()
    for it in NAV_ITEMS:
        k = it.get("permission_key")
        if k: keys.add(k)
        for sub in it.get("sub_items", []) or []:
            sk = sub.get("permission_key")
            if sk: keys.add(sk)
    upsert_permission_keys(sorted(keys))

# 掛載 Routers
app.include_router(auth_router)
app.include_router(permissions_router)
app.include_router(roles_router)
app.include_router(groups_router)


