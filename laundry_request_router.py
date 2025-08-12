import datetime
import os
from typing import Dict, Optional

import pandas as pd
from fastapi import APIRouter, Cookie, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse

# 從 app_utils 匯入共用的函式和物件
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


def get_employee_info(employee_id: str) -> Optional[Dict[str, str]]:
    """根據員工編號從 employees.xlsx 查找員工的部門、姓名和成本中心"""
    try:
        df = pd.read_excel(EMPLOYEES_FILE, sheet_name=EMPLOYEE_SHEET_NAME, dtype=str)
        df.columns = df.columns.str.strip()

        required_cols = ["員工編號", "部門", "姓名", "成本中心"]
        for col in required_cols:
            if col not in df.columns:
                raise ValueError(f"'{EMPLOYEES_FILE}' 中缺少必要的欄位: '{col}'")

        # ===== 核心修正 =====
        # 清理員工編號欄位，移除前後空白和可能存在的 '.0' 後綴
        df["員工編號"] = df["員工編號"].str.strip().str.replace(r"\.0$", "", regex=True)

        employee_record = df[df["員工編號"] == employee_id]

        if not employee_record.empty:
            record = employee_record.iloc[0]
            return {
                "department": record.get("部門"),
                "name": record.get("姓名"),
                "cost_center": record.get("成本中心"),
            }
        return None
    except FileNotFoundError:
        print(f"錯誤: 員工資料檔 '{EMPLOYEES_FILE}' 不存在。")
        return None
    except Exception as e:
        print(f"讀取員工資料時發生錯誤: {e}")
        return None


@router.get("/api/employee/{employee_id}", tags=["Laundry"])
async def get_employee_details(employee_id: str):
    """根據員工編號獲取員工詳細資訊 (姓名和部門)"""
    if not employee_id:
        raise HTTPException(status_code=400, detail="未提供員工編號")

    info = get_employee_info(employee_id)

    if info:
        return {"name": info.get("name"), "department": info.get("department")}
    else:
        raise HTTPException(status_code=404, detail="找不到該員工編號")


@router.get("/laundry/request", tags=["Laundry"])
async def get_laundry_form(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    """顯示洗衣服務申請表單頁面"""
    if not user:
        return RedirectResponse(url="/login")

    ctx = get_base_context(request, user, role, permissions)
    ctx["today"] = datetime.date.today().isoformat()
    ctx["clothing_items"] = CLOTHING_ITEMS
    return templates.TemplateResponse("laundry_request.html", ctx)


@router.post("/laundry/request", tags=["Laundry"])
async def submit_laundry_request(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    """處理洗衣服務申請表單的提交"""
    if not user:
        return RedirectResponse(url="/login")

    form_data = await request.form()
    request_date = form_data.get("date")
    employee_id = form_data.get("employee_id")

    if not request_date or not employee_id:
        ctx = get_base_context(request, user, role, permissions)
        ctx["error"] = "日期和員工編號為必填項目。"
        ctx["today"] = datetime.date.today().isoformat()
        ctx["clothing_items"] = CLOTHING_ITEMS
        return templates.TemplateResponse("laundry_request.html", ctx, status_code=400)

    employee_info = get_employee_info(employee_id)
    if not employee_info:
        ctx = get_base_context(request, user, role, permissions)
        ctx["error"] = f"找不到員工編號 '{employee_id}' 的資料，請確認輸入是否正確。"
        ctx["today"] = datetime.date.today().isoformat()
        ctx["clothing_items"] = CLOTHING_ITEMS
        return templates.TemplateResponse("laundry_request.html", ctx, status_code=400)

    data_to_save = {
        "日期": request_date,
        "部門": employee_info.get("department", "N/A"),
        "姓名": employee_info.get("name", "N/A"),
        "成本中心": employee_info.get("cost_center", "N/A"),
        "員工編號": employee_id,
        "登記人": user,
    }

    total_quantity = 0
    for item in CLOTHING_ITEMS:
        quantity_str = form_data.get(f"quantity_{item}", "0")
        try:
            quantity = int(quantity_str) if quantity_str else 0
            data_to_save[item] = quantity
            total_quantity += quantity
        except (ValueError, TypeError):
            data_to_save[item] = 0

    if total_quantity == 0:
        ctx = get_base_context(request, user, role, permissions)
        ctx["error"] = "請至少輸入一項送洗衣物的數量。"
        ctx["today"] = datetime.date.today().isoformat()
        ctx["clothing_items"] = CLOTHING_ITEMS
        return templates.TemplateResponse("laundry_request.html", ctx, status_code=400)

    try:
        columns = [
            "日期",
            "部門",
            "姓名",
            "員工編號",
            "成本中心",
            "登記人",
        ] + CLOTHING_ITEMS
        new_record_df = pd.DataFrame([data_to_save], columns=columns)
        df_to_write = new_record_df

        if os.path.exists(LAUNDRY_DATA_FILE):
            try:
                existing_df = pd.read_excel(
                    LAUNDRY_DATA_FILE, sheet_name=SHEET_NAME, engine="openpyxl"
                )
                df_to_write = pd.concat([existing_df, new_record_df], ignore_index=True)
            except Exception as read_error:
                print(
                    f"警告: 無法讀取現有的 '{LAUNDRY_DATA_FILE}' ({read_error})。將會建立新檔案。"
                )

        df_to_write.to_excel(
            LAUNDRY_DATA_FILE, sheet_name=SHEET_NAME, index=False, engine="openpyxl"
        )

    except Exception as e:
        print(f"寫入洗衣資料時發生嚴重錯誤: {e}")
        ctx = get_base_context(request, user, role, permissions)
        ctx["error"] = f"儲存資料時發生內部錯誤，請聯繫管理員。錯誤詳情: {e}"
        ctx["today"] = datetime.date.today().isoformat()
        ctx["clothing_items"] = CLOTHING_ITEMS
        return templates.TemplateResponse("laundry_request.html", ctx, status_code=500)

    return RedirectResponse(url="/laundry/request?success=true", status_code=303)
