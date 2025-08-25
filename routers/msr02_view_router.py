# routers/msr02_view_router.py
from fastapi import APIRouter, Request, Cookie
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
    FileResponse,
)
from typing import Optional, List, Tuple
import pandas as pd
import os
import io
import json

from app_utils import templates, get_base_context  # 沿用你專案的共用方法

router = APIRouter(tags=["Revenue"])

# 支援多個可能路徑，第一個存在者優先
EXCEL_CANDIDATES = [
    r"data/RV/MSR02.xlsx",
    r"data/MSR02.xlsx",
    r"MSR02.xlsx",
]


def _pick_excel_path() -> Optional[str]:
    for p in EXCEL_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _list_sheets(path: str) -> List[str]:
    try:
        xl = pd.ExcelFile(path)
        return xl.sheet_names
    except Exception:
        return []


def _load_excel(
    path: str, sheet_name: str, limit: Optional[int]
) -> Tuple[List[str], List[dict], int]:
    # 全欄位以字串載入，避免型別問題
    df = pd.read_excel(path, sheet_name=sheet_name, dtype=str)
    df = df.fillna("")
    total = len(df)
    if limit and limit > 0:
        df = df.head(limit)
    cols = [str(c) for c in df.columns]
    rows = df.to_dict(orient="records")
    return cols, rows, total


def _has_report_permission(
    role: Optional[str], permissions_json: Optional[str]
) -> bool:
    if role == "admin":
        return True
    try:
        perms = json.loads(permissions_json) if permissions_json else {}
        return bool(perms.get("report", False))
    except Exception:
        return False


@router.get("/msr02", response_class=HTMLResponse)
async def msr02_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    sheet: Optional[str] = None,  # 指定工作表名稱；未填就取第一個
    show_all: Optional[int] = 0,  # 0=只看前 N 筆，1=全部
    limit: Optional[int] = 300,  # 預設顯示前 300 筆
):
    # 登入檢查
    if not user:
        return RedirectResponse(url="/login")

    # 權限檢查（沿用 report 權限）
    if not _has_report_permission(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    excel_path = _pick_excel_path()
    error = None
    columns, records, total_rows = [], [], 0
    sheet_names: List[str] = []
    selected_sheet = sheet

    if not excel_path:
        error = (
            "找不到 MSR02.xlsx，請確認路徑是否存在：/mnt/data 或 ./data 或專案根目錄。"
        )
    else:
        sheet_names = _list_sheets(excel_path)
        if not sheet_names:
            error = "MSR02.xlsx 讀取失敗或沒有任何工作表。"
        else:
            if (not selected_sheet) or (selected_sheet not in sheet_names):
                selected_sheet = sheet_names[0]
            try:
                effective_limit = None if show_all == 1 else (limit or 300)
                columns, records, total_rows = _load_excel(
                    excel_path, selected_sheet, effective_limit
                )
            except Exception as e:
                error = f"讀取 Excel 發生錯誤：{e}"

    # 組裝 context（沿用你的共用 base context，側欄/抬頭都會正常）
    ctx = get_base_context(request, user, role, permissions)
    ctx.update(
        {
            "excel_path": excel_path,
            "sheet_names": sheet_names,
            "selected_sheet": selected_sheet,
            "columns": columns,
            "records": records,
            "total_rows": total_rows,
            "show_all": show_all,
            "limit": limit or 300,
            "error": error,
        }
    )
    return templates.TemplateResponse("msr02_view.html", ctx)


@router.get("/msr02/download/csv")
async def download_csv(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    sheet: Optional[str] = None,
):
    if not user:
        return RedirectResponse(url="/login")
    if not _has_report_permission(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    excel_path = _pick_excel_path()
    if not excel_path:
        return HTMLResponse("找不到 MSR02.xlsx", status_code=404)

    sheet_names = _list_sheets(excel_path)
    if not sheet_names:
        return HTMLResponse("Excel 讀取失敗", status_code=500)
    if (not sheet) or (sheet not in sheet_names):
        sheet = sheet_names[0]

    df = pd.read_excel(excel_path, sheet_name=sheet, dtype=str).fillna("")
    stream = io.StringIO()
    df.to_csv(stream, index=False, encoding="utf-8-sig")
    stream.seek(0)
    filename = f"MSR02_{sheet}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        iter([stream.getvalue()]), media_type="text/csv", headers=headers
    )


@router.get("/msr02/download/excel")
async def download_excel(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not _has_report_permission(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    excel_path = _pick_excel_path()
    if not excel_path:
        return HTMLResponse("找不到 MSR02.xlsx", status_code=404)

    # 直接送出原檔
    return FileResponse(excel_path, filename=os.path.basename(excel_path))
