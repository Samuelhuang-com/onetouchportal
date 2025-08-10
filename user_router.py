import pandas as pd
from fastapi import APIRouter, Request, Form, Cookie, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional

# 從 app_utils 匯入共用的函式和物件
from app_utils import get_base_context, templates, PERMISSION_FILE

# 建立一個新的 Router
router = APIRouter(prefix="/user", tags=["User"])


# 依賴項：檢查使用者是否登入
async def get_current_user(user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/", status_code=302)
    return user


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    user: str = Depends(get_current_user),  # 確保使用者已登入
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    """顯示修改密碼的頁面"""
    ctx = get_base_context(request, user, role, permissions)
    # 從 URL 查詢參數讀取訊息，以便在頁面上顯示
    ctx["error"] = request.query_params.get("error")
    ctx["success"] = request.query_params.get("success")
    return templates.TemplateResponse("change_password.html", ctx)


@router.post("/change-password")
async def handle_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    user: str = Depends(get_current_user),  # 確保使用者已登入
):
    """處理修改密碼的表單提交"""
    # 1. 驗證新密碼是否相符
    if new_password != confirm_password:
        return RedirectResponse(
            url="/user/change-password?error=新密碼兩次輸入不相符", status_code=302
        )

    # 2. 讀取權限檔案
    try:
        df = pd.read_excel(
            PERMISSION_FILE, sheet_name="employeesfiles", dtype=str
        ).fillna("")
    except FileNotFoundError:
        return RedirectResponse(
            url="/user/change-password?error=系統錯誤，找不到權限檔案", status_code=302
        )

    # 3. 找到目前登入的使用者
    user_index = df.index[df["loginname"] == user].tolist()

    if not user_index:
        return RedirectResponse(
            url="/user/change-password?error=找不到您的使用者帳號", status_code=302
        )

    user_index = user_index[0]

    # 4. 驗證目前的密碼是否正確
    if df.loc[user_index, "password"] != current_password:
        return RedirectResponse(
            url="/user/change-password?error=目前的密碼不正確", status_code=302
        )

    # 5. 更新密碼
    df.loc[user_index, "password"] = new_password

    # 6. 將更新後的 DataFrame 寫回 Excel 檔案
    try:
        # 使用 openpyxl 引擎來寫入，以更好地支援 .xlsx 格式
        df.to_excel(PERMISSION_FILE, index=False, engine="openpyxl")
    except Exception as e:
        print(f"寫入 Excel 檔案時發生錯誤: {e}")
        return RedirectResponse(
            url=f"/user/change-password?error=系統錯誤，無法儲存新密碼", status_code=302
        )

    # 7. 成功後重導向，並附上成功訊息
    return RedirectResponse(
        url="/user/change-password?success=密碼已成功更新！", status_code=302
    )
