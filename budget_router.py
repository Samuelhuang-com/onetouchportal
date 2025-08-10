import json
import os
from fastapi import APIRouter, Request, Form, Cookie, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional, Dict

# 從 app_utils 匯入共用的函式和物件
from app_utils import get_base_context, templates, BUDGETS_FILE

# 建立一個新的 Router
router = APIRouter()


def get_budgets() -> Dict:
    """從 JSON 檔案讀取預算，若檔案不存在則回傳預設值"""
    default_budgets = {
        "mtd_revenue": 2800000,
        "day_revenue": 93500,
        "mtd_bev": 200000,
        "day_bev": 6500,
    }
    if not os.path.exists(BUDGETS_FILE):
        return default_budgets
    try:
        with open(BUDGETS_FILE, "r", encoding="utf-8") as f:
            # 合併預設值與檔案內容，確保所有鍵都存在
            budgets_from_file = json.load(f)
            default_budgets.update(budgets_from_file)
            return default_budgets
    except (json.JSONDecodeError, FileNotFoundError):
        return default_budgets


def save_budgets(budgets: Dict):
    """將預算儲存至 JSON 檔案"""
    os.makedirs(os.path.dirname(BUDGETS_FILE), exist_ok=True)
    with open(BUDGETS_FILE, "w", encoding="utf-8") as f:
        json.dump(budgets, f, ensure_ascii=False, indent=4)


# 建立一個依賴項，用來檢查管理預算的權限
async def check_budget_permission(
    role: Optional[str] = Cookie(None), permissions: Optional[str] = Cookie(None)
) -> bool:
    if role == "admin":
        return True
    try:
        perms_dict = json.loads(permissions) if permissions else {}
        return perms_dict.get("manage_budget", False)
    except (json.JSONDecodeError, TypeError):
        return False


@router.get("/budgets/manage", response_class=HTMLResponse, tags=["Budgets"])
async def manage_budgets_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_budget_permission),
):
    """顯示預算管理頁面"""
    if not user:
        return RedirectResponse(url="/", status_code=302)
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ctx = get_base_context(request, user, role, permissions)
    ctx["budgets"] = get_budgets()
    ctx["success_message"] = request.query_params.get("success")
    return templates.TemplateResponse("manage_budgets.html", ctx)


@router.post("/budgets/update", tags=["Budgets"])
async def update_budgets_entry(
    request: Request,
    mtd_revenue: int = Form(...),
    day_revenue: int = Form(...),
    mtd_bev: int = Form(...),
    day_bev: int = Form(...),
    user: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_budget_permission),
):
    """處理更新預算的請求"""
    if not user or not has_permission:
        return RedirectResponse(url="/", status_code=302)

    new_budgets = {
        "mtd_revenue": mtd_revenue,
        "day_revenue": day_revenue,
        "mtd_bev": mtd_bev,
        "day_bev": day_bev,
    }

    save_budgets(new_budgets)

    return RedirectResponse(url="/budgets/manage?success=true", status_code=302)
