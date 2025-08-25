# routers/rv_analysis_router.py
from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import pandas as pd
import numpy as np
import re

router = APIRouter()

# 專案路徑與資料檔
REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_FILE = REPO_ROOT / "data" / "RV" / "MSR02.xlsx"

# 在 router 內自行建立 templates（避免 app.state 依賴與載入順序問題）
for _cand in [
    REPO_ROOT / "templates",
    Path.cwd() / "templates",
    Path(__file__).resolve().parent / "templates",
]:
    if _cand.exists():
        TEMPLATE_DIR = _cand
        break
else:
    TEMPLATE_DIR = REPO_ROOT / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# 欄位同義詞（擴充中文欄名）
FIELD_SYNONYMS = {
    "date": [
        "傳票日期",
        "日期",
        "營業日期",
        "交易日期",
        "帳務日期",
        "憑證日期",
        "報表日期",
        "入住日期",
        "離店日期",
        "date",
        "trans_date",
        "posting_date",
        "business_date",
        "document_date",
        "doc_date",
        "booking_date",
    ],
    # rooms_sold：優先「售房合計」，若沒有就用「總住房數」
    "rooms_sold": [
        "售房合計",
        "總住房數",
        "rooms_sold",
        "sold_rooms",
        "間夜",
        "房晚",
        "roomsold",
        "rooms sold",
    ],
    "rooms_avail": [
        "可售房數",
        "rooms_avail",
        "available_rooms",
        "可售",
        "供應房晚",
        "rooms available",
        "room_supply",
    ],
    "room_rev": [
        "總房租",
        "room_revenue",
        "rm_rev",
        "房租收入",
        "roomrev",
        "rmrevenue",
        "房價收入",
    ],
    "fb_rev": ["fb_revenue", "f&b", "beverage", "餐飲收入", "餐飲營業額"],
    "other_rev": ["other_revenue", "misc_revenue", "其他收入", "其他營收"],
    "channel": ["channel", "ota", "booking_source", "來源", "通路"],
    "rate_plan": ["rate_plan", "plan", "方案", "價格方案"],
    "segment": ["segment", "market_segment", "市場", "market", "客群", "來源別"],
}


# ---------------- JSON 安全化 ----------------
def _safe_float(x, default=0.0):
    try:
        v = float(x)
    except Exception:
        return default
    if np.isnan(v) or np.isinf(v):
        return default
    return v


def _series_to_list(
    s: pd.Series, as_percent: bool = False, round_ndigits=None, null_ok=True
):
    s = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)
    if as_percent:
        s = s * 100
    if round_ndigits is not None:
        s = s.round(round_ndigits)
    out = []
    for v in s.tolist():
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            out.append(None if null_ok else 0)
        else:
            out.append(float(v))
    return out


# ---------------- 輔助：欄名、日期解析 ----------------
def _lower_map_columns(df: pd.DataFrame):
    return {str(col).strip().lower(): col for col in df.columns}


def _excel_serial_to_datetime(s: pd.Series) -> pd.Series:
    s_num = pd.to_numeric(s, errors="coerce")
    dt = pd.to_datetime(s_num, unit="D", origin="1899-12-30", errors="coerce")
    mask = (dt >= pd.Timestamp("2000-01-01")) & (dt <= pd.Timestamp("2100-12-31"))
    return dt.where(mask)


def _parse_yyyymmdd(x):
    try:
        xs = str(int(x))
    except Exception:
        xs = str(x)
    xs = re.sub(r"\D", "", xs)
    if len(xs) == 8:
        try:
            return pd.to_datetime(xs, format="%Y%m%d", errors="coerce")
        except Exception:
            return pd.NaT
    return pd.NaT


def _parse_date_series(s: pd.Series) -> pd.Series:
    """
    日期解析順序：
    1) 直接 to_datetime（字串/原生 datetime）
    2) 若落在 1970 年附近（常見於把整數當 ns），改用 YYYYMMDD 解析
    3) 仍有缺者 → Excel 序號（1899-12-30 起算）
    """
    dt = pd.to_datetime(s, errors="coerce", infer_datetime_format=True)
    # 若大多數年份 <= 1975，視為「整數被當成 ns」的狀況 → 用 YYYYMMDD 重解
    if (dt.dropna().dt.year <= 1975).mean() > 0.5:
        dt2 = s.apply(_parse_yyyymmdd)
        if dt2.notna().mean() > 0.5:
            dt = dt2
    need = dt.isna()
    if need.any():
        dt_excel = _excel_serial_to_datetime(s)
        dt.loc[need] = dt_excel[need]
    return dt


def _probe_date_score(series: pd.Series) -> float:
    dt = _parse_date_series(series)
    return float(dt.notna().mean())


def _choose_date_column(df: pd.DataFrame, preferred: str | None) -> str | None:
    cols = list(df.columns)
    if preferred and preferred in cols:
        return preferred

    lower_to_orig = _lower_map_columns(df)
    # 同義詞優先
    for alias in FIELD_SYNONYMS["date"]:
        key = alias.strip().lower()
        if key in lower_to_orig:
            return lower_to_orig[key]

    # 名稱關鍵字 + 內容評分
    name_candidates = [
        c
        for c in cols
        if any(
            k in str(c).lower()
            for k in [
                "date",
                "day",
                "日",
                "期",
                "傳票",
                "交易",
                "營業",
                "憑證",
                "帳務",
                "報表",
                "入住",
                "離店",
            ]
        )
    ]
    candidates = list(dict.fromkeys(name_candidates + cols))  # 去重保序
    scored = [(c, _probe_date_score(df[c])) for c in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    for c, sc in scored:
        if sc >= 0.5:
            return c
    return None


# ---------------- 規格化、計算 ----------------
def _read_excel(sheet_name: str | None = None) -> pd.DataFrame:
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"找不到檔案：{DATA_FILE}")
    return pd.read_excel(DATA_FILE, sheet_name=sheet_name or 0, engine="openpyxl")


def _normalize_columns(df: pd.DataFrame, date_col: str | None = None) -> pd.DataFrame:
    chosen_date = _choose_date_column(df, preferred=date_col)
    if not chosen_date:
        raise ValueError("找不到日期欄位，請於頁面指定日期欄位後再試。")

    # 日期
    date_series = _parse_date_series(df[chosen_date])

    out = pd.DataFrame()
    out["date"] = date_series

    lower_to_orig = _lower_map_columns(df)

    def pick_num(std_name, default=0):
        # rooms_sold：若同時存在「售房合計」與「總住房數」，優先採用「售房合計」
        targets = FIELD_SYNONYMS.get(std_name, [])
        for alias in targets:
            key = alias.strip().lower()
            if key in lower_to_orig:
                col = lower_to_orig[key]
                return pd.to_numeric(df[col], errors="coerce").fillna(default)
        return pd.Series([default] * len(df))

    def pick_text(std_name):
        targets = FIELD_SYNONYMS.get(std_name, [])
        for alias in targets:
            key = alias.strip().lower()
            if key in lower_to_orig:
                col = lower_to_orig[key]
                return df[col].astype(str).fillna("")
        return ""

    out["rooms_sold"] = pick_num("rooms_sold")
    out["rooms_avail"] = pick_num("rooms_avail")
    out["room_rev"] = pick_num("room_rev")
    out["fb_rev"] = pick_num("fb_rev", default=0)
    out["other_rev"] = pick_num("other_rev", default=0)
    out["channel"] = pick_text("channel")
    out["rate_plan"] = pick_text("rate_plan")
    out["segment"] = pick_text("segment")

    # 指標：分母為 0 → NaN，前端以 null 顯示缺口
    out["occ"] = np.where(
        out["rooms_avail"] > 0, out["rooms_sold"] / out["rooms_avail"], np.nan
    )
    out["adr"] = np.where(
        out["rooms_sold"] > 0, out["room_rev"] / out["rooms_sold"], np.nan
    )
    out["revpar"] = np.where(
        out["rooms_avail"] > 0, out["room_rev"] / out["rooms_avail"], np.nan
    )
    out["total_rev"] = out["room_rev"] + out["fb_rev"] + out["other_rev"]

    out = out.dropna(subset=["date"]).sort_values("date")
    return out


def slice_by_date(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start:
        df = df[df["date"] >= pd.to_datetime(start)]
    if end:
        df = df[df["date"] <= pd.to_datetime(end)]
    return df


def kpis(df: pd.DataFrame) -> dict:
    occ_avg = df["occ"].astype(float)
    adr_avg = df["adr"].astype(float)
    rvp_avg = df["revpar"].astype(float)
    return {
        "total_rev": _safe_float(df["total_rev"].sum(), 0.0),
        "room_rev": _safe_float(df["room_rev"].sum(), 0.0),
        "occ": _safe_float(np.nanmean(occ_avg), 0.0),
        "adr": _safe_float(np.nanmean(adr_avg), 0.0),
        "revpar": _safe_float(np.nanmean(rvp_avg), 0.0),
    }


def timeseries(df: pd.DataFrame) -> dict:
    g = df.groupby("date", as_index=False).agg(
        total_rev=("total_rev", "sum"),
        room_rev=("room_rev", "sum"),
        occ=("occ", "mean"),
        adr=("adr", "mean"),
        revpar=("revpar", "mean"),
    )
    for c in ["total_rev", "room_rev", "occ", "adr", "revpar"]:
        g[c] = pd.to_numeric(g[c], errors="coerce").replace([np.inf, -np.inf], np.nan)
    g["ma7_rev"] = g["total_rev"].rolling(7, min_periods=1).mean()
    g["ma28_rev"] = g["total_rev"].rolling(28, min_periods=1).mean()

    return {
        "date": g["date"].dt.strftime("%Y-%m-%d").tolist(),
        "total_rev": _series_to_list(g["total_rev"], round_ndigits=0, null_ok=True),
        "room_rev": _series_to_list(g["room_rev"], round_ndigits=0, null_ok=True),
        "occ": _series_to_list(
            g["occ"], as_percent=True, round_ndigits=1, null_ok=True
        ),
        "adr": _series_to_list(g["adr"], round_ndigits=0, null_ok=True),
        "revpar": _series_to_list(g["revpar"], round_ndigits=0, null_ok=True),
        "ma7_rev": _series_to_list(g["ma7_rev"], round_ndigits=0, null_ok=True),
        "ma28_rev": _series_to_list(g["ma28_rev"], round_ndigits=0, null_ok=True),
    }


def top_groups(df: pd.DataFrame, by: str, n=10):
    if by not in df.columns:
        return []
    g = (
        df.groupby(by, dropna=False)
        .agg(
            rooms_sold=("rooms_sold", "sum"),
            room_rev=("room_rev", "sum"),
            adr=("adr", "mean"),
            revpar=("revpar", "mean"),
        )
        .reset_index()
        .sort_values("room_rev", ascending=False)
        .head(n)
    )
    out = g.to_dict(orient="records")
    for r in out:
        r["adr"] = (
            0 if pd.isna(r["adr"]) or np.isinf(r["adr"]) else round(float(r["adr"]), 0)
        )
        r["revpar"] = (
            0
            if pd.isna(r["revpar"]) or np.isinf(r["revpar"])
            else round(float(r["revpar"]), 0)
        )
        r["room_rev"] = (
            0
            if pd.isna(r["room_rev"]) or np.isinf(r["room_rev"])
            else round(float(r["room_rev"]), 0)
        )
        r["rooms_sold"] = (
            0
            if pd.isna(r["rooms_sold"]) or np.isinf(r["rooms_sold"])
            else int(r["rooms_sold"])
        )
    return out


# ---------------- Routes ----------------
@router.get("/rv")
async def rv_overview_page(request: Request):
    return templates.TemplateResponse("rv_overview.html", {"request": request})


@router.get("/rv/api/columns")
async def rv_columns(sheet: str | None = None):
    try:
        raw = _read_excel(sheet_name=sheet)
        cols = list(map(str, raw.columns))
        scored = [(c, _probe_date_score(raw[c])) for c in raw.columns]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = [
            {"column": str(c), "score": round(float(sc), 3)} for c, sc in scored[:10]
        ]
        best = top[0]["column"] if top and top[0]["score"] >= 0.5 else None
        return JSONResponse(
            {"columns": cols, "date_candidates": top, "suggested_date_col": best}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/rv/api/overview")
async def rv_api_overview(
    start: str | None = None,
    end: str | None = None,
    date_col: str | None = Query(default=None, description="手動指定日期欄位"),
    sheet: str | None = Query(default=None, description="工作表名稱，可選"),
):
    try:
        raw = _read_excel(sheet_name=sheet)
        df = _normalize_columns(raw, date_col=date_col)
        df = slice_by_date(df, start, end)
        data = {
            "kpi": kpis(df),
            "ts": timeseries(df),
            "top_channel": top_groups(df, "channel", n=8),
            "top_rate_plan": top_groups(df, "rate_plan", n=8),
            "top_segment": top_groups(df, "segment", n=8),
            "available_dims": [
                c for c in ["channel", "rate_plan", "segment"] if c in df.columns
            ],
        }
        return JSONResponse(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
