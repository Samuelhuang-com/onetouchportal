# rv_analysis_router.py
from fastapi import APIRouter, Request, Cookie, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from typing import Optional, Dict, Any, List
import pandas as pd
import numpy as np
import os
from app_utils import templates, get_base_context

# 原本的寫法 (錯誤的)


router = APIRouter(tags=["RV分析"])

DEFAULT_FILE = "data/RV/MSR02.xlsx"
DEFAULT_SHEET = 0
HEADER_ROW = 4  # 你的檔案第5列開始是表頭

NUM_COLS = ["住房率", "平均房價", "可售總金額", "總房租"]


def _load_rv_df(file_path: str, sheet=DEFAULT_SHEET) -> pd.DataFrame:
    df = pd.read_excel(file_path, sheet_name=sheet, header=HEADER_ROW)
    # 只保留日期是連續數字的列：20250801
    df = df[df["日期"].apply(lambda x: str(x).isdigit())].copy()
    # 轉數字
    for col in NUM_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # 安全計算 RevPAR
    # RevPAR = 總房租 / 可售房數 => 用 可售總金額 / 平均房價 反推可售房數，避免沒有可售房欄位
    denom = (df["可售總金額"] / df["平均房價"]).replace([np.inf, -np.inf], np.nan)
    df["RevPAR"] = np.where(denom.fillna(0) > 0, df["總房租"] / denom, 0.0)

    # 轉日期型別 & 排序
    df["日期"] = pd.to_datetime(df["日期"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["日期"]).sort_values("日期").reset_index(drop=True)
    return df


def _summary(df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    def pack(series: pd.Series) -> Dict[str, float]:
        return {
            "avg": float(series.mean() if not series.empty else 0),
            "max": float(series.max() if not series.empty else 0),
            "min": float(series.min() if not series.empty else 0),
            "std": float(series.std(ddof=0) if series.size > 1 else 0),
        }

    return {
        "revenue": pack(df["總房租"]),
        "occ": pack(df["住房率"]),
        "adr": pack(df["平均房價"]),
        "revpar": pack(df["RevPAR"]),
    }


def _insights(df: pd.DataFrame) -> Dict[str, Any]:
    # 偵測波動：用 z-score 找出尖峰與低谷（以營收與入住率為主）
    out = {}
    for key, col in [("revenue", "總房租"), ("occ", "住房率")]:
        s = df[col].astype(float)
        mu, sd = s.mean(), s.std(ddof=0)
        if sd and sd > 0:
            z = (s - mu) / sd
            peaks_idx = z[z >= 1.2].index.tolist()
            dips_idx = z[z <= -1.2].index.tolist()
        else:
            peaks_idx, dips_idx = [], []
        out[key] = {
            "peaks": [df.loc[i, "日期"].strftime("%Y-%m-%d") for i in peaks_idx],
            "dips": [df.loc[i, "日期"].strftime("%Y-%m-%d") for i in dips_idx],
        }
    # 大致的「起跑日」：第一天四大指標任一 > 0
    start_idx = df[
        (df["總房租"] > 0)
        | (df["住房率"] > 0)
        | (df["平均房價"] > 0)
        | (df["RevPAR"] > 0)
    ].index
    start_date = (
        df.loc[start_idx[0], "日期"].strftime("%Y-%m-%d") if len(start_idx) else None
    )
    return {"peaks_dips": out, "start_date": start_date}


@router.get("/rv/analysis", response_class=HTMLResponse)
async def rv_analysis_page(
    request: Request,
    file: Optional[str] = Query(
        None, description="自訂 RV 檔案路徑 (預設 data/RV/MSR02.xlsx)"
    ),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission: bool = Depends(check_permission("report")),
):
    if not user:
        return RedirectResponse(url="/")
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    file_path = file or DEFAULT_FILE
    if not os.path.exists(file_path):
        # 檔案不存在也顯示頁面，但給錯誤訊息
        ctx = get_base_context(request, user, role, permissions)
        ctx.update({"error": f"找不到檔案：{file_path}"})
        return templates.TemplateResponse("rv_analysis.html", ctx)

    df = _load_rv_df(file_path)
    if df.empty:
        ctx = get_base_context(request, user, role, permissions)
        ctx.update({"error": "檔案無有效資料", "date_from": None, "date_to": None})
        return templates.TemplateResponse("rv_analysis.html", ctx)

    summary = _summary(df)
    insight = _insights(df)

    # Chart.js 資料
    chart_labels = df["日期"].dt.strftime("%Y-%m-%d").tolist()
    chart_data = {
        "revenue": df["總房租"].round(0).tolist(),
        "occ": df["住房率"].round(2).tolist(),
        "adr": df["平均房價"].round(0).tolist(),
        "revpar": df["RevPAR"].round(0).tolist(),
    }

    ctx = get_base_context(request, user, role, permissions)
    ctx.update(
        dict(
            date_from=df["日期"].min().strftime("%Y-%m-%d"),
            date_to=df["日期"].max().strftime("%Y-%m-%d"),
            chart_labels=chart_labels,
            chart_data=chart_data,
            summary=summary,
            insight=insight,
            src_file=os.path.basename(file_path),
        )
    )
    return templates.TemplateResponse("rv_analysis.html", ctx)


@router.get("/rv/analysis/data")
async def rv_analysis_data(file: Optional[str] = Query(None)):
    """若前端想以 AJAX 取數據，可打這支。"""
    file_path = file or DEFAULT_FILE
    if not os.path.exists(file_path):
        return JSONResponse({"error": f"找不到檔案：{file_path}"}, status_code=404)
    df = _load_rv_df(file_path)
    if df.empty:
        return JSONResponse({"error": "檔案無有效資料"}, status_code=400)
    summary = _summary(df)
    insight = _insights(df)
    payload = {
        "labels": df["日期"].dt.strftime("%Y-%m-%d").tolist(),
        "revenue": df["總房租"].round(0).tolist(),
        "occ": df["住房率"].round(2).tolist(),
        "adr": df["平均房價"].round(0).tolist(),
        "revpar": df["RevPAR"].round(0).tolist(),
        "summary": summary,
        "insight": insight,
        "date_from": df["日期"].min().strftime("%Y-%m-%d"),
        "date_to": df["日期"].max().strftime("%Y-%m-%d"),
        "src_file": os.path.basename(file_path),
    }
    return JSONResponse(payload)
