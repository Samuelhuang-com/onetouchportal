import os
import pandas as pd
from fastapi import Request
from fastapi.templating import Jinja2Templates
from typing import Optional, List, Dict
import json
import locale

# --- 檔案路徑常數 ---
DAILY_LOG_FILE = "data/daily_log.xlsx"
CONTRACTS_FILE = "data/contracts.xlsx"
CONTRACT_ATTACHMENT_DIR = "data/ContractFile"
PERMISSION_FILE = "data/Permissioncontrol.xlsx"
ANNOUNCEMENTS_FILE = "data/announcements.json"
BUDGETS_FILE = "data/budgets.json"
# --- 模板引擎設定 (包含百分比過濾器) ---


def percent_filter(value):
    """將數字轉換為百分比格式的字串，例如 0.95 -> '95.00%'"""
    try:
        # 嘗試將傳入的值格式化為帶兩位小數的百分比
        return "{:.2%}".format(float(value))
    except (ValueError, TypeError):
        # 如果值無法轉換 (例如是 None 或空字串)，則直接返回原值
        return value


def number_format_filter(value):
    """將數字加上千分位符號"""
    try:
        # 使用 locale 來做本地化的數字格式化
        locale.setlocale(locale.LC_ALL, "")
        return locale.format_string("%d", int(value), grouping=True)
    except (ValueError, TypeError):
        return value


# 建立 Jinja2 模板實例
templates = Jinja2Templates(directory="templates")

# ★ 核心修正：將自訂的 percent_filter 函式註冊到模板環境中
# 這樣在 HTML 裡才能使用 {{ my_number | percent }}
templates.env.filters["percent"] = percent_filter
templates.env.filters["number"] = number_format_filter

# --- 導覽列項目定義 ---
NAV_ITEMS = [
    {"name": "主控台", "url": "/dashboard", "icon": "🏠"},
    {"name": "行銷活動", "url": "/events", "icon": "📅", "permission_key": "events"},
    {
        "name": "訂位總覽",
        "url": "/reservations",
        "icon": "📖",
        "permission_key": "reservations",
    },
    {
        "name": "營運報表",
        "icon": "📊",
        "sub_items": [
            {"name": "Power BI 總覽", "url": "/powerbi", "permission_key": "powerbi"},
            {"name": "營業日誌報表", "url": "/report", "permission_key": "report"},
            {
                "name": "維護營業日誌",
                "url": "/daily_log/manage",
                "permission_key": "report",
            },
            {
                "name": "預算管理",
                "url": "/budgets/manage",
                "permission_key": "manage_budget",
            },
        ],
    },
    {
        "name": "資料管理",
        "icon": "📂",
        "sub_items": [
            {
                "name": "公告管理",
                "url": "/announcements/manage",
                "permission_key": "announcements",
            },
            {"name": "員工通訊錄", "url": "/employees", "permission_key": "employees"},
            {"name": "合約管理", "url": "/contracts", "permission_key": "contracts"},
            {"name": "最新班表", "url": "/hr_folder", "permission_key": "hr_folder"},
            {"name": "帳號管理", "url": "/users", "permission_key": "users"},
            {"name": "修改密碼", "url": "/user/change-password", "icon": "🔑"},
        ],
    },
    # --- 新增的內部知識庫項目 ---
    {
        "name": "內部知識庫",
        "icon": "📚",
        "sub_items": [
            {
                "name": "SOP 文件",
                "url": "/knowledge/sop",
                "permission_key": "knowledge_base",
            },
            {
                "name": "緊急應變流程",
                "url": "/knowledge/emergency",
                "permission_key": "knowledge_base",
            },
            {
                "name": "表單下載",
                "url": "/knowledge/forms",
                "permission_key": "knowledge_base",
            },
            {
                "name": "常見問題 (FAQ)",
                "url": "/knowledge/faq",
                "permission_key": "knowledge_base",
            },
            {
                "name": "關鍵字搜尋",
                "url": "/knowledge/search",
                "permission_key": "knowledge_base",
            },
        ],
    },
     {"name": "功能總覽", "url": "/home", "icon": "🧭"},
]


def get_visible_nav_items(role: str, permissions: Dict[str, bool]) -> List[Dict]:
    """
    根據角色和權限，遞迴地決定要顯示哪些導覽列項目。
    """
    visible_items = []
    for item in NAV_ITEMS:
        if "sub_items" in item:
            visible_sub_items = []
            for sub_item in item["sub_items"]:
                key = sub_item.get("permission_key")
                if role == "admin" or permissions.get(key, False):
                    visible_sub_items.append(sub_item)
            if visible_sub_items:
                new_item = item.copy()
                new_item["sub_items"] = visible_sub_items
                visible_items.append(new_item)
        else:
            key = item.get("permission_key")
            if not key or role == "admin" or permissions.get(key, False):
                visible_items.append(item)
    return visible_items


def get_base_context(
    request: Request,
    user: Optional[str],
    role: Optional[str],
    permissions_str: Optional[str] = "{}",
) -> Dict:
    """
    取得所有頁面共用的 context 字典。
    """
    permissions = {}
    try:
        permissions = json.loads(permissions_str) if permissions_str else {}
    except (json.JSONDecodeError, TypeError):
        permissions = {}

    visible_nav = get_visible_nav_items(role or "guest", permissions)

    return {
        "request": request,
        "nav_items": visible_nav,
        "current_path": request.url.path,
        "user": user,
        "role": role,
        "permissions": permissions,
    }
