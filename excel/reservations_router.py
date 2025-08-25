from fastapi import APIRouter, Request, Query, Cookie
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from typing import Optional, Dict, Any, List
import pandas as pd
import numpy as np
import os
from datetime import datetime

from app_utils import templates, get_base_context

router = APIRouter(prefix="/excel", tags=["Excel Analysis"])

DATA_PATH_CANDIDATES = [
    "data/RV/MSR02.xlsx",  # project default
    os.path.join(os.getcwd(), "data", "MSR02.xlsx"),  # absolute
    "data/RV/MSR02.xlsx",  # fallback (chat environment)
]


def _first_existing(paths: List[str]) -> Optional[str]:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def _parse_msr02() -> pd.DataFrame:
    path = _first_existing(DATA_PATH_CANDIDATES)
    if not path:
        raise FileNotFoundError("找不到 MSR02.xlsx，請放到 data/RV/MSR02.xlsx")
    raw = pd.read_excel(path, header=None)
    # 尋找標頭列：「日期」字樣
    header_row = None
    for i in range(min(15, len(raw))):
        if str(raw.iloc[i, 0]).strip() == "日期":
            header_row = i
            break
    if header_row is None:
        # fallback 已知樣式：第3列 (index=3)
        header_row = 3
    start_row = header_row + 2  # 資料起始列

    # 欄位映射 (依據提供的檔案結構)
    COL = {
        "date": 0,
        "weekday": 2,
        "occ_total_pct": 3,  # 以「總房間數」為分母的住房率（%）
        "occ_avail_pct": 4,  # 以「可售房數」為分母的住房率（%）
        "adr_total": 5,  # 以「總住房數」為分母的平均房價
        "adr_sold": 6,  # 以「售房合計」為分母的平均房價
        "avail_revenue": 7,  # 可售總金額
        "room_revenue": 8,  # 總房租（實際房租收入）
        "total_rooms": 9,  # 總房間數
        "ooo": 10,
        "mbk": 11,
        "hus": 12,
        "comp": 13,
        "rooms_available": 14,
        "rooms_sold": 15,
        "rooms_occupied": 16,  # 含COMP等
        "fit": 17,
        "git": 18,
        "guests": 19,
        "no_bed": 20,
    }
    df = raw.iloc[start_row:, :].copy()

    # 僅保留日期為YYYYMMDD的列
    def to_date(x):
        s = str(x).strip()
        return s if s.isdigit() and len(s) == 8 else None

    df = df[df.iloc[:, COL["date"]].apply(to_date).notna()].copy()
    # 轉型
    df.rename(columns={i: k for k, i in COL.items()}, inplace=True)
    num_cols = [c for c in COL.keys() if c not in ("date", "weekday")]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    # 日期格式
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    # 派生指標
    df["occ_rate"] = np.where(
        df["rooms_available"] > 0, df["rooms_sold"] / df["rooms_available"], 0.0
    )
    df["revpar"] = np.where(
        df["rooms_available"] > 0, df["room_revenue"] / df["rooms_available"], 0.0
    )
    df["adr"] = np.where(
        df["rooms_sold"] > 0, df["room_revenue"] / df["rooms_sold"], 0.0
    )
    df["ooo_rate"] = np.where(df["total_rooms"] > 0, df["ooo"] / df["total_rooms"], 0.0)
    return df.reset_index(drop=True)


def _filter_by_date(
    df: pd.DataFrame, start: Optional[str], end: Optional[str]
) -> pd.DataFrame:
    if start:
        start_dt = pd.to_datetime(start)
        df = df[df["date"] >= start_dt]
    if end:
        end_dt = pd.to_datetime(end)
        df = df[df["date"] <= end_dt]
    return df


@router.get("/analysis", response_class=HTMLResponse)
async def excel_analysis_page(
    request: Request,
    start: Optional[str] = Query(None, description="開始日期 YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="結束日期 YYYY-MM-DD"),
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/login")
    ctx = get_base_context(request, user, role, permissions)
    try:
        df = _parse_msr02()
        df_filtered = _filter_by_date(df, start, end)
        # 預設顯示最近14天
        if df_filtered.empty:
            last14 = df.sort_values("date").tail(14)
            df_filtered = last14
        # KPI 匯總
        kpi = {
            "period_start": df_filtered["date"].min().date().isoformat(),
            "period_end": df_filtered["date"].max().date().isoformat(),
            "room_revenue": float(df_filtered["room_revenue"].sum()),
            "rooms_sold": int(df_filtered["rooms_sold"].sum()),
            "rooms_available": int(df_filtered["rooms_available"].sum()),
            "occ_rate": float(
                (df_filtered["rooms_sold"].sum() / df_filtered["rooms_available"].sum())
                if df_filtered["rooms_available"].sum()
                else 0
            ),
            "adr": float(
                (df_filtered["room_revenue"].sum() / df_filtered["rooms_sold"].sum())
                if df_filtered["rooms_sold"].sum()
                else 0
            ),
            "revpar": float(
                (
                    df_filtered["room_revenue"].sum()
                    / df_filtered["rooms_available"].sum()
                )
                if df_filtered["rooms_available"].sum()
                else 0
            ),
            "ooo_rate": float(
                (df_filtered["ooo"].sum() / df_filtered["total_rooms"].sum())
                if df_filtered["total_rooms"].sum()
                else 0
            ),
            "fit": int(df_filtered["fit"].sum()),
            "git": int(df_filtered["git"].sum()),
        }
        ctx["kpi"] = kpi
        # 預留給前端 fetch JSON 的 API
        return templates.TemplateResponse("excel_analysis.html", ctx)
    except FileNotFoundError as e:
        ctx["error"] = str(e)
        return templates.TemplateResponse("excel_analysis.html", ctx)


@router.get("/analysis/data", response_class=JSONResponse)
async def excel_analysis_data(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    df = _parse_msr02()
    df = _filter_by_date(df, start, end).sort_values("date")
    payload = {
        "dates": df["date"].dt.strftime("%Y-%m-%d").tolist(),
        "occ_rate": (df["occ_rate"] * 100).round(2).tolist(),
        "adr": df["adr"].round(2).tolist(),
        "revpar": df["revpar"].round(2).tolist(),
        "revenue": df["room_revenue"].round(0).astype(int).tolist(),
        "ooo": df["ooo"].astype(int).tolist(),
        "rooms_available": df["rooms_available"].astype(int).tolist(),
        "rooms_sold": df["rooms_sold"].astype(int).tolist(),
        "fit": df["fit"].astype(int).tolist(),
        "git": df["git"].astype(int).tolist(),
        "kpi": {
            "room_revenue": float(df["room_revenue"].sum()),
            "rooms_sold": int(df["rooms_sold"].sum()),
            "rooms_available": int(df["rooms_available"].sum()),
            "occ_rate": float(
                (df["rooms_sold"].sum() / df["rooms_available"].sum())
                if df["rooms_available"].sum()
                else 0
            ),
            "adr": float(
                (df["room_revenue"].sum() / df["rooms_sold"].sum())
                if df["rooms_sold"].sum()
                else 0
            ),
            "revpar": float(
                (df["room_revenue"].sum() / df["rooms_available"].sum())
                if df["rooms_available"].sum()
                else 0
            ),
            "ooo_rate": float(
                (df["ooo"].sum() / df["total_rooms"].sum())
                if df["total_rooms"].sum()
                else 0
            ),
            "fit": int(df["fit"].sum()),
            "git": int(df["git"].sum()),
        },
    }
    return JSONResponse(payload)
