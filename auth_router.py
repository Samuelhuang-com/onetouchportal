import json
from fastapi import APIRouter, Request, Form, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional
import pandas as pd
import numpy as np

# 從 app_utils 匯入共用的函式和物件
from app_utils import templates, PERMISSION_FILE

router = APIRouter()


def verify_user(username, password):
    """
    驗證使用者身份 (更穩健的版本)。
    """
    try:
        # ★ 核心修正：讀取 Excel 時，直接將所有欄位視為字串(string)處理
        # 這可以避免 pandas 自動判斷型別所帶來的潛在問題 (例如 True vs "TRUE")
        df = pd.read_excel(PERMISSION_FILE, sheet_name="employeesfiles", dtype=str)
        # 將讀取後的 NaN (空值) 替換為空字串
        df = df.fillna("")

        # 現在所有欄位都是字串，可以安全地進行比對
        login_match = df["loginname"].str.strip() == username
        pass_match = df["password"].str.strip() == password
        status_match = df["status"].str.strip().str.upper() == "TRUE"

        user_record = df[login_match & pass_match & status_match]

        if not user_record.empty:
            # 將找到的第一筆紀錄轉為字典
            return user_record.iloc[0].to_dict()
        else:
            return None
    except FileNotFoundError:
        print(f"錯誤：權限檔案 '{PERMISSION_FILE}' 不存在。")
        return None
    except Exception as e:
        print(f"驗證使用者時發生嚴重錯誤: {e}")
        return None


@router.get("/", response_class=HTMLResponse, tags=["Authentication"])
async def read_root(request: Request):
    """顯示登入頁面"""
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login", tags=["Authentication"])
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """處理登入請求，並將權限存入 Cookie"""
    user_data = verify_user(username, password)

    if user_data:
        resp = RedirectResponse(url="/dashboard", status_code=302)

        permission_columns = [
            "events",
            "reservations",
            "powerbi",
            "report",
            "employees",
            "contracts",
            "hr_folder",
            "users",
            "announcements",
            "view_announcements",
            "manage_budget",
        ]

        # ★ 核心修正：因為所有資料都已是字串，所以判斷邏輯可以大幅簡化且更可靠
        permissions = {
            col: user_data.get(col, "").strip().upper() == "TRUE"
            for col in permission_columns
        }

        # ★★★ 除錯用：在伺服器控制台印出生成的權限，確認是否正確 ★★★
        print(f"\n--- DEBUG: Login Auth (Final Fix) ---")
        print(f"使用者 '{username}' 的權限已生成: {permissions}")
        print(f"-------------------------------------\n")

        resp.set_cookie(key="user", value=user_data.get("loginname", ""))
        resp.set_cookie(key="role", value=user_data.get("role", "guest"))
        resp.set_cookie(key="permissions", value=json.dumps(permissions))
        return resp
    else:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "帳號或密碼錯誤，或帳號已被停用。"},
        )


@router.get("/logout", tags=["Authentication"])
async def logout():
    """處理登出請求，清除所有相關 Cookie"""
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie("user")
    resp.delete_cookie("role")
    resp.delete_cookie("permissions")
    return resp
