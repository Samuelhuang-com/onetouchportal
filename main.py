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

# 匯入各個功能的 router
from report_router import router as report_router
from contract_router import router as contract_router
from daily_log_router import router as daily_log_router
from auth_router import router as auth_router
from reservations_router import router as reservations_router
from announcement_router import router as announcement_router
from budget_router import router as budget_router
from user_router import router as user_router

# --- App 初始化 ---
app = FastAPI(title="飯店管理系統")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 掛載所有功能的 router
app.include_router(report_router)
app.include_router(contract_router)
app.include_router(auth_router)
app.include_router(daily_log_router)
app.include_router(reservations_router)
app.include_router(announcement_router)
app.include_router(budget_router)
app.include_router(user_router)

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
    permissions: Optional[str] = Cookie(None)
):
    # 如果你要登入後才看得到，可以加判斷
    if not user:
        return RedirectResponse(url="/login")  # 或你的登入頁面路徑

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
        return RedirectResponse(url="/login")
    
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
    can_view_announcements = (role == 'admin') or ctx['permissions'].get('view_announcements', False)
    if can_view_announcements:
        try:
            if os.path.exists(ANNOUNCEMENTS_FILE):
                with open(ANNOUNCEMENTS_FILE, "r", encoding="utf-8") as f:
                    all_announcements = json.load(f)
                latest_announcements = sorted(all_announcements, key=lambda x: x.get('timestamp', ''), reverse=True)[:5]
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

