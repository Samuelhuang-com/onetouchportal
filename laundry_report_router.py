import datetime
from typing import Optional, List
import pandas as pd
from fastapi import APIRouter, Cookie, Depends, Request, Query
from fastapi.responses import HTMLResponse

# 從共用模組匯入
from app_utils import get_base_context, templates

router = APIRouter()

# --- 常數定義 ---
LAUNDRY_DATA_FILE = "data/laundry_data.xlsx"
EMPLOYEES_FILE = "data/employees.xlsx"
SHEET_NAME = "data"
EMPLOYEE_SHEET_NAME = "在職員工"

CLOTHING_ITEMS = [
    "廚衣",
    "廚帽",
    "背心",
    "領巾",
    "襯衫",
    "圍裙",
    "領帶",
    "西上",
    "西褲",
    "洋裝",
    "女裙",
    "大衣",
    "夾克",
    "毛衣",
    "領結",
]


@router.get("/laundry/report", response_class=HTMLResponse, tags=["Laundry Report"])
async def get_laundry_report(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
):
    """
    顯示洗衣統計報表頁面，並根據提供的日期區間處理查詢。
    """
    ctx = get_base_context(request, user, role, permissions)
    ctx["start_date"] = start_date
    ctx["end_date"] = end_date
    ctx["results"] = []
    ctx["grand_totals"] = {}
    ctx["clothing_items"] = CLOTHING_ITEMS
    ctx["error"] = None

    if start_date and end_date:
        try:
            # 1. 讀取員工資料，建立 員工編號 -> 部門 的對應表
            emp_df = pd.read_excel(
                EMPLOYEES_FILE, sheet_name=EMPLOYEE_SHEET_NAME, dtype=str
            )
            emp_df.columns = emp_df.columns.str.strip()

            if "員工編號" not in emp_df.columns or "部門" not in emp_df.columns:
                raise ValueError(
                    f"'{EMPLOYEES_FILE}' 中缺少 '員工編號' 或 '部門' 欄位。"
                )

            # ===== 核心修正 =====
            # 清理員工編號欄位，移除前後空白和可能存在的 '.0' 後綴
            emp_df["員工編號"] = (
                emp_df["員工編號"].str.strip().str.replace(r"\.0$", "", regex=True)
            )

            emp_map = emp_df.drop_duplicates(subset=["員工編號"]).set_index("員工編號")[
                "部門"
            ]

            # 2. 讀取洗衣資料
            laundry_df = pd.read_excel(
                LAUNDRY_DATA_FILE, sheet_name=SHEET_NAME, engine="openpyxl"
            )

            # 3. 資料清理與轉換
            laundry_df["日期"] = pd.to_datetime(laundry_df["日期"], errors="coerce")
            laundry_df.dropna(subset=["日期", "員工編號"], inplace=True)

            # ===== 核心修正 =====
            # 同樣清理洗衣資料中的員工編號，以確保能成功匹配
            laundry_df["員工編號"] = (
                laundry_df["員工編號"]
                .astype(str)
                .str.strip()
                .str.replace(r"\.0$", "", regex=True)
            )
            laundry_df[CLOTHING_ITEMS] = (
                laundry_df[CLOTHING_ITEMS]
                .apply(pd.to_numeric, errors="coerce")
                .fillna(0)
            )

            # 4. 關聯部門資料
            laundry_df["部門"] = laundry_df["員工編號"].map(emp_map).fillna("未知部門")

            # 5. 根據日期區間篩選資料
            mask = (laundry_df["日期"].dt.date >= pd.to_datetime(start_date).date()) & (
                laundry_df["日期"].dt.date <= pd.to_datetime(end_date).date()
            )
            filtered_df = laundry_df.loc[mask]

            # 6. 計算與統計
            if not filtered_df.empty:
                report_data = filtered_df.groupby("部門")[CLOTHING_ITEMS].sum()
                report_data = report_data[report_data.sum(axis=1) > 0]

                if not report_data.empty:
                    report_data["部門總計"] = report_data.sum(axis=1)
                    grand_totals = report_data.sum().to_dict()
                    report_data = report_data.sort_values(
                        by="部門總計", ascending=False
                    ).astype(int)

                    ctx["results"] = report_data.reset_index().to_dict("records")
                    ctx["grand_totals"] = {k: int(v) for k, v in grand_totals.items()}

        except FileNotFoundError as e:
            ctx["error"] = f"錯誤：找不到資料檔 '{e.filename}'。"
        except Exception as e:
            print(f"報表生成錯誤: {e}")
            ctx["error"] = f"處理資料時發生錯誤，請檢查 Excel 檔案格式或內容是否正確。"

    return templates.TemplateResponse("laundry_report.html", ctx)
