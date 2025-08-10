import json
import os
from fastapi import APIRouter, Request, Form, Cookie, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional, List, Dict
from datetime import datetime

# 從 app_utils 匯入共用的函式和物件
from app_utils import get_base_context, templates, ANNOUNCEMENTS_FILE

# 建立一個新的 Router
router = APIRouter()


def read_announcements() -> List[Dict]:
    """從 JSON 檔案讀取公告"""
    if not os.path.exists(ANNOUNCEMENTS_FILE):
        return []
    try:
        with open(ANNOUNCEMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_announcements(announcements: List[Dict]):
    """將公告儲存至 JSON 檔案"""
    # 確保 data 資料夾存在
    os.makedirs(os.path.dirname(ANNOUNCEMENTS_FILE), exist_ok=True)
    with open(ANNOUNCEMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(announcements, f, ensure_ascii=False, indent=4)


# 建立一個依賴項，用來檢查管理公告的權限
async def check_announcement_permission(
    role: Optional[str] = Cookie(None), permissions: Optional[str] = Cookie(None)
) -> bool:
    if role == "admin":
        return True
    try:
        perms_dict = json.loads(permissions) if permissions else {}
        return perms_dict.get("announcements", False)
    except (json.JSONDecodeError, TypeError):
        return False


@router.get(
    "/announcements/manage", response_class=HTMLResponse, tags=["Announcements"]
)
async def manage_announcements_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_announcement_permission),
):
    """顯示公告管理頁面"""
    if not user:
        return RedirectResponse(url="/", status_code=302)
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    ctx = get_base_context(request, user, role, permissions)
    # 讀取所有公告並依時間排序
    all_announcements = sorted(
        read_announcements(), key=lambda x: x["timestamp"], reverse=True
    )
    ctx["announcements"] = all_announcements
    return templates.TemplateResponse("manage_announcements.html", ctx)


@router.post("/announcements/add", tags=["Announcements"])
async def add_announcement_entry(
    request: Request,
    title: str = Form(...),
    content: str = Form(...),
    user: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_announcement_permission),
):
    """處理新增公告的請求"""
    if not user or not has_permission:
        return RedirectResponse(url="/", status_code=302)

    announcements = read_announcements()

    # 建立新的公告資料
    new_announcement = {
        "id": len(announcements) + 1,  # 簡單的 ID 生成方式
        "title": title,
        "content": content,
        "author": user,
        "timestamp": datetime.now().isoformat(),
    }

    announcements.append(new_announcement)
    save_announcements(announcements)

    return RedirectResponse(url="/announcements/manage", status_code=302)


@router.post("/announcements/delete", tags=["Announcements"])
async def delete_announcement_entry(
    request: Request,
    announcement_id: int = Form(...),
    user: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_announcement_permission),
):
    """處理刪除公告的請求"""
    if not user or not has_permission:
        return RedirectResponse(url="/", status_code=302)

    announcements = read_announcements()
    # 過濾掉要刪除的公告
    announcements_to_keep = [
        ann for ann in announcements if ann.get("id") != announcement_id
    ]

    save_announcements(announcements_to_keep)

    return RedirectResponse(url="/announcements/manage", status_code=302)
