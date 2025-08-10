from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional
import pandas as pd
import os

# 從 app_utils 匯入需要的項目
from app_utils import get_base_context, templates, DAILY_LOG_FILE

router = APIRouter()


# --- 營業日誌相關函式 ---
def read_daily_log():
    """讀取營業日誌 Excel 檔案"""
    if not os.path.exists(DAILY_LOG_FILE) or os.path.getsize(DAILY_LOG_FILE) == 0:
        return pd.DataFrame(
            columns=[
                "Date",
                "Meal",
                "Covers",
                "FoodRev",
                "BevRev",
                "OthersRev",
                "SvcCharge",
            ]
        )
    return pd.read_excel(DAILY_LOG_FILE, engine="openpyxl")


def save_daily_log(df: pd.DataFrame):
    """儲存營業日誌 DataFrame 至 Excel 檔案"""
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values(by="Date", ascending=False).reset_index(drop=True)
    df.to_excel(DAILY_LOG_FILE, index=False, engine="openpyxl")


# --- Daily Log Management Routes ---


@router.get("/daily_log/manage", response_class=HTMLResponse)
async def manage_daily_log_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/", status_code=302)

    ctx = get_base_context(request, user, role, permissions)
    # 權限檢查 (維護功能跟隨 report 權限)
    if role != "admin" and not ctx["permissions"].get("report"):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    df = read_daily_log()
    # ★ 核心修正：直接將 DataFrame 轉為字典列表。
    # 不再預先格式化日期，將其作為 datetime 物件傳遞給模板。
    records = df.reset_index().to_dict("records")

    ctx["records"] = records
    return templates.TemplateResponse("manage_daily_log.html", ctx)


@router.post("/daily_log/add")
async def add_daily_log_entry(
    request: Request,
    Date: str = Form(...),
    Meal: str = Form(...),
    Covers: int = Form(...),
    FoodRev: int = Form(...),
    BevRev: int = Form(...),
    OthersRev: int = Form(...),
    SvcCharge: int = Form(...),
    user: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/", status_code=302)

    df = read_daily_log()

    new_record = pd.DataFrame(
        [
            {
                "Date": pd.to_datetime(Date),
                "Meal": Meal,
                "Covers": Covers,
                "FoodRev": FoodRev,
                "BevRev": BevRev,
                "OthersRev": OthersRev,
                "SvcCharge": SvcCharge,
            }
        ]
    )

    df = pd.concat([df, new_record], ignore_index=True)
    save_daily_log(df)

    return RedirectResponse(url="/daily_log/manage", status_code=302)


@router.post("/daily_log/delete")
async def delete_daily_log_entry(
    request: Request, record_index: int = Form(...), user: Optional[str] = Cookie(None)
):
    if not user:
        return RedirectResponse(url="/", status_code=302)

    df = read_daily_log()

    if 0 <= record_index < len(df):
        df = df.drop(index=record_index).reset_index(drop=True)
        save_daily_log(df)

    return RedirectResponse(url="/daily_log/manage", status_code=302)
