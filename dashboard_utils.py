import os
import json
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict

# 檔案路徑常數
DAILY_LOG_FILE = "data/daily_log.xlsx"
BUDGETS_FILE = "data/budgets.json"

def read_daily_log() -> pd.DataFrame:
    """從 Excel 讀取並快取營業日誌"""
    if not os.path.exists(DAILY_LOG_FILE) or os.path.getsize(DAILY_LOG_FILE) == 0:
        return pd.DataFrame()
    return pd.read_excel(DAILY_LOG_FILE, engine='openpyxl')

def get_budgets() -> Dict:
    """從 JSON 檔案讀取預算"""
    default_budgets = {
        "mtd_revenue": 0, "day_revenue": 0,
        "mtd_bev": 0, "day_bev": 0
    }
    if not os.path.exists(BUDGETS_FILE):
        return default_budgets
    try:
        with open(BUDGETS_FILE, "r", encoding="utf-8") as f:
            budgets_from_file = json.load(f)
            default_budgets.update(budgets_from_file)
            return default_budgets
    except (json.JSONDecodeError, FileNotFoundError):
        return default_budgets

def get_dashboard_kpis() -> Dict:
    """
    計算並回傳儀表板所需的所有關鍵指標 (KPIs)。
    """
    df = read_daily_log()
    budgets = get_budgets()
    
    if df.empty:
        return {
            "yesterday_revenue": 0,
            "mtd_revenue": 0,
            "daily_budget": budgets.get("day_revenue", 0),
            "monthly_budget": budgets.get("mtd_revenue", 0),
            "mtd_achievement_rate": 0
        }

    df["TotalFB"] = df[["FoodRev", "BevRev", "OthersRev", "SvcCharge"]].sum(axis=1)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    
    today = datetime.now().date()
    # ★ 核心修正：新增 yesterday 的日期計算
    yesterday = today - timedelta(days=1)
    month_start = today.replace(day=1)

    # 計算昨日營收
    yesterday_df = df[df["Date"] == yesterday]
    yesterday_revenue = yesterday_df["TotalFB"].sum()
    
    # 計算本月累計營收 (MTD)
    mtd_df = df[(df["Date"] >= month_start) & (df["Date"] <= today)]
    mtd_revenue = mtd_df["TotalFB"].sum()
    
    monthly_budget = budgets.get("mtd_revenue", 0)
    
    # 計算本月達成率
    mtd_achievement_rate = (mtd_revenue / monthly_budget) if monthly_budget else 0
    
    # 回傳所有計算好的指標
    return {
        "yesterday_revenue": yesterday_revenue,
        "mtd_revenue": mtd_revenue,
        "daily_budget": budgets.get("day_revenue", 0),
        "monthly_budget": monthly_budget,
        "mtd_achievement_rate": mtd_achievement_rate
    }
