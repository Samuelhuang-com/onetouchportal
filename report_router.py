from fastapi import APIRouter, Request, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional, Dict
from datetime import datetime
import pandas as pd
import math
import os
import json

# 從 app_utils 匯入需要的項目
from app_utils import get_base_context, templates, DAILY_LOG_FILE, BUDGETS_FILE

# 建立一個新的 Router
router = APIRouter()


# --- Helper Functions ---
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


def get_budgets() -> Dict:
    """從 JSON 檔案讀取預算，若檔案不存在則回傳預設值"""
    default_budgets = {"mtd_revenue": 0, "day_revenue": 0, "mtd_bev": 0}
    if not os.path.exists(BUDGETS_FILE):
        return default_budgets
    try:
        with open(BUDGETS_FILE, "r", encoding="utf-8") as f:
            budgets_from_file = json.load(f)
            default_budgets.update(budgets_from_file)
            return default_budgets
    except (json.JSONDecodeError, FileNotFoundError):
        return default_budgets


@router.get("/report", response_class=HTMLResponse)
async def report_page(
    request: Request,
    date: Optional[str] = None,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")

    ctx = get_base_context(request, user, role, permissions)

    if role != "admin" and not ctx["permissions"].get("report"):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    # ★ 核心修正：從檔案動態讀取預算
    budgets = get_budgets()
    BUDGET_MTD_REVENUE = budgets.get("mtd_revenue", 0)
    BUDGET_DAY_REVENUE = budgets.get("day_revenue", 0)
    BUDGET_MTD_BEV = budgets.get("mtd_bev", 0)

    df = read_daily_log()
    if df.empty:
        ctx["error"] = "報表沒有任何資料可供顯示。"
        return templates.TemplateResponse("report.html", ctx)

    df["TotalFB"] = df[["FoodRev", "BevRev", "OthersRev", "SvcCharge"]].sum(axis=1)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    available_dates = sorted(df["Date"].astype(str).unique(), reverse=True)

    if not available_dates:
        ctx["error"] = "報表沒有任何有效的日期資料。"
        return templates.TemplateResponse("report.html", ctx)

    report_date_str = date if date else available_dates[0]
    report_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()

    month_start = report_date.replace(day=1)
    today_df = df[df["Date"] == report_date].copy()
    mtd_df = df[(df["Date"] >= month_start) & (df["Date"] <= report_date)].copy()

    MEAL_MAP = {
        "Breakfast": "早餐",
        "Lunch": "午餐",
        "Afternoon Tea": "下午茶",
        "Dinner": "晚餐",
    }
    today_df["Meal_Chinese"] = today_df["Meal"].map(MEAL_MAP)
    today_df["AvgCheck"] = (today_df["TotalFB"] / today_df["Covers"]).fillna(0).round()

    total_today_revenue = today_df["TotalFB"].sum()
    mtd_revenue = mtd_df["TotalFB"].sum()
    mtd_bev_revenue = mtd_df["BevRev"].sum()

    rates = {
        "mtd_rate": mtd_revenue / BUDGET_MTD_REVENUE if BUDGET_MTD_REVENUE else 0,
        "day_rate": (
            total_today_revenue / BUDGET_DAY_REVENUE if BUDGET_DAY_REVENUE else 0
        ),
        "bev_rate": mtd_bev_revenue / BUDGET_MTD_BEV if BUDGET_MTD_BEV else 0,
        "accumulated_mtd_rate": (
            mtd_revenue / (BUDGET_DAY_REVENUE * report_date.day)
            if BUDGET_DAY_REVENUE and report_date.day > 0
            else 0
        ),
    }

    df_trend = (
        mtd_df.groupby("Date").agg({"TotalFB": "sum", "Covers": "sum"}).reset_index()
    )
    df_trend["AvgCheck"] = (df_trend["TotalFB"] / df_trend["Covers"]).fillna(0).round()
    chart_data = {
        "labels": [d.strftime("%m/%d") for d in df_trend["Date"]],
        "total": df_trend["TotalFB"].fillna(0).tolist(),
        "avgcheck": df_trend["AvgCheck"].fillna(0).tolist(),
    }

    ctx.update(
        {
            "available_dates": available_dates,
            "report_date": report_date_str,
            "branch": "板石咖啡廳",
            "records": today_df.to_dict("records"),
            "totals": {
                "Covers": int(today_df["Covers"].sum()),
                "FoodRev": int(today_df["FoodRev"].sum()),
                "BevRev": int(today_df["BevRev"].sum()),
                "OthersRev": int(today_df["OthersRev"].sum()),
                "SvcCharge": int(today_df["SvcCharge"].sum()),
                "TotalFB": int(total_today_revenue),
                "AvgCheck": (
                    math.floor(total_today_revenue / today_df["Covers"].sum())
                    if today_df["Covers"].sum() > 0
                    else 0
                ),
            },
            "mtd_revenue": int(mtd_revenue),
            "rates": rates,
            "chart_data": chart_data,
            "budgets": budgets,  # 將預算也傳給模板，方便顯示
        }
    )
    return templates.TemplateResponse("report.html", ctx)
