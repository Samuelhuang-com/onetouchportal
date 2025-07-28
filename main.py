# main.py
from fastapi import (  # pyright: ignore[reportMissingImports]
    FastAPI,
    Request,
    Form,
    Cookie,
)  # pyright: ignore[reportMissingImports]
from fastapi.responses import (  # pyright: ignore[reportMissingImports]
    HTMLResponse,
    RedirectResponse,
)  # pyright: ignore[reportMissingImports]
from fastapi.staticfiles import StaticFiles  # pyright: ignore[reportMissingImports]
from fastapi.templating import Jinja2Templates  # pyright: ignore[reportMissingImports]
from typing import Optional
import csv
import json
import pandas as pd  # pyright: ignore[reportMissingModuleSource]
import requests  # pyright: ignore[reportMissingModuleSource] # 用於發送網路請求
import io  # 用於將字串當作檔案讀取

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- 資料讀取 ---
try:
    with open("data/users.json", "r", encoding="utf-8") as f:
        USERS = json.load(f)
except FileNotFoundError:
    USERS = {}

# --- 導覽列項目 ---
NAV_ITEMS = [
    {"name": "主控台", "url": "/dashboard", "icon": "🏠", "admin_only": False},
    {"name": "行銷活動", "url": "/events", "icon": "📅", "admin_only": False},
    {"name": "員工通訊錄", "url": "/employees", "icon": "👥", "admin_only": False},
    {"name": "Power BI 報表", "url": "/powerbi", "icon": "📊", "admin_only": False},
    {"name": "inline訂位", "url": "/inline", "icon": "📊", "admin_only": False},
    {"name": "帳號管理", "url": "/users", "icon": "⚙️", "admin_only": True},
]


def get_base_context(request: Request, user: str):
    """取得所有頁面都需要共用的 context"""
    return {
        "request": request,
        "nav_items": NAV_ITEMS,
        "current_path": request.url.path,
        "user": user,
        "role": USERS.get(user, {}).get("role", "guest"),
    }


# --- 登入與登出 ---
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user_data = USERS.get(username)
    if user_data and user_data["password"] == password:
        response = RedirectResponse(url="/dashboard", status_code=302)
        response.set_cookie(key="user", value=username)
        return response
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "帳號或密碼錯誤"}
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("user")
    return response


# --- 新增的全站搜尋路由 ---
@app.get("/search", response_class=HTMLResponse)
async def search_site(request: Request, q: str, user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/", status_code=302)

    query_lower = q.lower()
    event_results = []
    employee_results = []

    # 搜尋 events.csv
    try:
        with open("data/events.csv", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if any(query_lower in str(value).lower() for value in row.values()):
                    event_results.append(row)
    except FileNotFoundError:
        print("Warning: data/events.csv not found.")

    # 搜尋 employees.xlsx
    try:
        df = pd.read_excel("data/employees.xlsx")
        df_str = df.astype(str)
        mask = df_str.apply(
            lambda r: r.str.lower().str.contains(query_lower, na=False).any(), axis=1
        )
        employee_results = df[mask].to_dict(orient="records")
    except FileNotFoundError:
        print("Warning: data/employees.xlsx not found.")

    context = get_base_context(request, user)
    context["query"] = q
    context["event_results"] = event_results
    context["employee_results"] = employee_results

    return templates.TemplateResponse("search_results.html", context)


# --- 主要頁面 ---
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/")
    context = get_base_context(request, user)
    return templates.TemplateResponse("dashboard.html", context)


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/")
    events = []
    try:
        with open("data/events.csv", newline="", encoding="utf-8") as f:
            events = list(csv.DictReader(f))
    except FileNotFoundError:
        pass
    context = get_base_context(request, user)
    context["events"] = events
    return templates.TemplateResponse("events.html", context)


@app.get("/events/add", response_class=HTMLResponse)
async def add_event_form(request: Request, user: Optional[str] = Cookie(None)):
    if not user or USERS.get(user, {}).get("role") != "admin":
        return RedirectResponse(url="/dashboard")
    context = get_base_context(request, user)
    return templates.TemplateResponse("add_event.html", context)


@app.post("/events/add")
async def add_event_submit(
    request: Request,
    日期: str = Form(...),
    活動名稱: str = Form(...),
    負責人: str = Form(...),
    地點: str = Form(...),
    網址: Optional[str] = Form(None),
    活動截止日: Optional[str] = Form(None),
):
    user = request.cookies.get("user")
    if not user or USERS.get(user, {}).get("role") != "admin":
        return RedirectResponse(url="/dashboard")

    fieldnames = ["日期", "活動名稱", "負責人", "地點", "網址", "活動截止日"]
    new_row = {
        "日期": 日期,
        "活動名稱": 活動名稱,
        "負責人": 負責人,
        "地點": 地點,
        "網址": 網址,
        "活動截止日": 活動截止日,
    }

    try:
        # 檢查檔案是否存在且非空
        file_exists = False
        try:
            with open("data/events.csv", "r", newline="", encoding="utf-8") as f:
                if f.read(1):
                    file_exists = True
        except FileNotFoundError:
            pass

        with open("data/events.csv", "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(new_row)
    except Exception as e:
        print(f"Error writing to events.csv: {e}")

    return RedirectResponse(url="/events", status_code=302)


@app.get("/employees", response_class=HTMLResponse)
async def employees_page(request: Request, user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/")
    records = []
    try:
        df = pd.read_excel("data/employees.xlsx")
        records = df.to_dict(orient="records")
    except FileNotFoundError:
        pass
    context = get_base_context(request, user)
    context["records"] = records
    return templates.TemplateResponse("employees.html", context)


@app.get("/powerbi", response_class=HTMLResponse)
async def powerbi_page(request: Request, user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/")
    context = get_base_context(request, user)
    return templates.TemplateResponse("powerbi.html", context)


@app.get("/users", response_class=HTMLResponse)
async def manage_users_page(request: Request, user: Optional[str] = Cookie(None)):
    if not user or USERS.get(user, {}).get("role") != "admin":
        return RedirectResponse(url="/dashboard")
    context = get_base_context(request, user)
    context["users"] = USERS
    return templates.TemplateResponse("manage_users.html", context)


# --- Google Sheet 頁面路由 (重要修改) ---
@app.get("/inline", response_class=HTMLResponse)
async def inline_page(request: Request, user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/", status_code=302)

    csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSgOdq4_USDnP8QJuMkwVMUmhFU2EnPpBQsHwogYILolTWXKNS34E6AOH8wbl-syx1t1L9v6sTk-GKq/pub?output=csv"
    sheet_data = []
    headers = []
    try:
        response = requests.get(csv_url)
        response.raise_for_status()

        csv_content = response.content.decode("utf-8")
        csv_file = io.StringIO(csv_content)

        reader = csv.reader(csv_file)

        # --- 這裡是修改的關鍵 ---
        all_headers = next(reader)  # 讀取所有的標頭
        headers = all_headers[:10]  # 只選取前 10 個標頭 (A 到 J 欄)

        for row in reader:
            # 只選取前 10 個欄位的資料，並與標頭打包成字典
            sheet_data.append(dict(zip(headers, row[:10])))
        # --- 修改結束 ---

    except requests.exceptions.RequestException as e:
        print(f"Error fetching Google Sheet CSV: {e}")
    except Exception as e:
        print(f"Error processing CSV data: {e}")

    context = get_base_context(request, user)
    context["sheet_headers"] = headers
    context["sheet_data"] = sheet_data
    return templates.TemplateResponse("inline.html", context)
