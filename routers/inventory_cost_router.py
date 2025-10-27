# routers/inventory_cost_router.py
from fastapi import APIRouter, Request, UploadFile, File, HTTPException, Depends, Cookie
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
    HTMLResponse,
)
from pathlib import Path
import io
import time
from fastapi import Query


# ✅ 和 Rooms 一致：用共用 templates / context / 權限
from app_utils import get_base_context, templates, check_permission

from routers.services.inventory_cost_service import (
    compute_cost_from_excel,
    export_category_csv,
    CostCache,
    compute_breakfast_bpg,
)


router = APIRouter(prefix="/inventory", tags=["Inventory Cost"])

# ---- simple in-memory cache & current file path ----
CACHE = CostCache(ttl_seconds=300)
CURRENT_FILE_PATH: Path | None = None

# 專案根目錄（routers/ 的上一層）
BASE_DIR = Path(__file__).resolve().parent.parent
# 絕對路徑，避免工作目錄不同造成找不到檔案
DATA_DIR = BASE_DIR / "data" / "uploads"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 支援多種大小寫與前綴：IVR05.XLSX、IVR05.xlsx、IVR05_*.xlsx
DEFAULT_NAMES = ["IVR05.XLSX", "IVR05.xlsx", "ivr05.xlsx"]

def _norm_date_str(s: str) -> str:
    s = (s or "").strip().replace("/", "-").replace(".", "-")
    try:
        y, m, d = [int(x) for x in s.split("-")]
        return f"{y:04d}-{m:02d}-{d:02d}"
    except Exception:
        return s

def _probe_default_file() -> Path | None:
    # 1) 直接比對常見檔名
    for name in DEFAULT_NAMES:
        p = DATA_DIR / name
        if p.exists():
            return p
    # 2) 嘗試找最新的 IVR05*.xls/xlsx
    candidates = sorted(
        DATA_DIR.glob("IVR05*.xls*"), key=lambda x: x.stat().st_mtime, reverse=True
    )
    if candidates:
        return candidates[0]
    # 3) 不分大小寫掃描包含 ivr05 的檔名
    for p in DATA_DIR.iterdir():
        if (
            p.is_file()
            and "ivr05" in p.name.lower()
            and p.suffix.lower() in (".xlsx", ".xls")
        ):
            return p
    return None


# 決定實際使用的 Excel 檔（優先使用最近上傳；否則用 data/uploads 的預設檔）
def _resolve_current_file() -> Path:
    global CURRENT_FILE_PATH
    if CURRENT_FILE_PATH and CURRENT_FILE_PATH.exists():
        return CURRENT_FILE_PATH
    probed = _probe_default_file()
    if probed and probed.exists():
        return probed
    raise HTTPException(
        status_code=404,
        detail=f"找不到 Excel：請把檔案放到 {DATA_DIR} / IVR05.xlsx 再重整。",
    )


# ---- Page（完全比照 Rooms：Cookie + 權限 + 共用 templates）----
@router.get("/cost", response_class=HTMLResponse)
async def cost_dashboard(
    request: Request,
    user: str | None = Cookie(None),
    role: str | None = Cookie(None),
    permissions: str | None = Cookie(None),
    has_permission: bool = Depends(check_permission("inventory_cost_view")),
):
    if not has_permission:
        return RedirectResponse(url="/dashboard?error=permission_denied")
    ctx = get_base_context(request, user, role, permissions)
    return templates.TemplateResponse("inventory/cost_dashboard.html", ctx)


# ---- APIs ----
@router.get("/api/cost")
async def api_cost():
    path = _resolve_current_file()
    payload = CACHE.get(path)
    if payload is None:
        payload = compute_cost_from_excel(path)
        CACHE.set(path, payload)
    return JSONResponse(payload)


@router.post("/api/reload")
async def api_reload():
    CACHE.clear()
    return JSONResponse({"ok": True})


@router.post("/cost/upload")
async def upload_cost_file(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="請上傳 Excel 檔 (.xlsx/.xls)")

    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_name = f"IVR05_{ts}.xlsx"
    target = DATA_DIR / safe_name

    content = await file.read()
    target.write_bytes(content)

    global CURRENT_FILE_PATH
    CURRENT_FILE_PATH = target
    CACHE.clear()
    return RedirectResponse(url="/inventory/cost", status_code=303)


@router.get("/api/export")
async def api_export_csv():
    path = _resolve_current_file()
    payload = CACHE.get(path)
    if payload is None:
        payload = compute_cost_from_excel(path)
        CACHE.set(path, payload)

    csv_bytes = export_category_csv(payload)
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=inventory_category_summary.csv"
        },
    )


# 讀取 covers 的檔案（可選）：data/covers/2025-08.json 內容 {"covers": 12345}
def _try_load_covers(period: str | None) -> int | None:
    if not period:
        return None
    covers_dir = DATA_DIR.parent / "covers"
    # 先找 2025-09.json / .txt
    for ext in ("json", "txt"):
        p = covers_dir / f"{period}.{ext}"
        if p.exists():
            try:
                if ext == "json":
                    import json
                    with open(p, "r", encoding="utf-8") as f:
                        obj = json.load(f)
                    v = obj.get("covers")
                    return int(v) if v is not None else None
                else:
                    v = p.read_text(encoding="utf-8").strip()
                    return int(v)
            except Exception:
                continue
    # 找不到 → 用每日 CSV 加總
    return _try_load_month_covers_from_daily(period)



@router.get("/api/breakfast_bpg")
async def api_breakfast_bpg(
    period: str | None = Query(None, description="YYYY-MM，例如 2025-08"),
    covers: int | None = Query(None, description="早餐人次(本月總計)"),
):
    """
    方向A：早餐每客成本（BPG）
    用 IVR05 估算耗用(A) × 對照表比例 → Breakfast_COGS，再 / covers 得到 BPG。
    covers 未提供時，嘗試讀 data/covers/{period}.json 或 .txt。
    """
    path = _resolve_current_file()
    if covers is None:
        covers = _try_load_covers(period)
    payload = compute_breakfast_bpg(path, covers=covers, period=period)
    return JSONResponse(payload)

# --- 工具：讀取月人次（沿用你之前的 covers 檔） ---
def _try_load_covers(period: str | None) -> int | None:
    if not period:
        return None
    covers_dir = DATA_DIR.parent / "covers"  # data/covers
    for ext in ("json", "txt"):
        p = covers_dir / f"{period}.{ext}"
        if p.exists():
            try:
                if ext == "json":
                    import json
                    with open(p, "r", encoding="utf-8") as f:
                        obj = json.load(f)
                    v = obj.get("covers", None)
                    return int(v) if v is not None else None
                else:
                    v = p.read_text(encoding="utf-8").strip()
                    return int(v)
            except Exception:
                continue
    return None


@router.get("/api/breakfast_bpg_daily")
async def api_breakfast_bpg_daily(
    date: str = Query(..., description="YYYY-MM-DD"),
    covers_day: int | None = Query(None, description="當日早餐人次（可選）"),
    covers_month: int | None = Query(None, description="本月早餐人次（可選；若無會嘗試從 data/covers/{YYYY-MM}.json 讀取）"),
    weekend_uplift: float = Query(0.06, description="週末相對平日的加成（預設 +6%）"),
    weekday_uplift: float = Query(0.0, description="平日加成（預設 0%）"),
):
    """
    以「月度早餐COGS」/「月人次」得到 base BPG，
    再依該日是否週末套用加成係數，並做月平均歸一化（避免整月被系數放大/縮小）。
    """
    from datetime import datetime, date as date_cls
    import calendar

    # ✅ 正規化日期
    date = _norm_date_str(date)
    
    # 決定該月
    period = date[:7]
    path = _resolve_current_file()

    # ✅ 在這裡補上「若沒傳 covers_day 就從 daily CSV 讀」
    if covers_day is None and date:
        covers_day = _try_load_daily_covers(date)

    # 月人次
    month_covers = covers_month or _try_load_covers(period)
    # 拿月度早餐成本（方向A的計算）
    monthly = compute_breakfast_bpg(path, covers=month_covers, period=period)
    # 若沒有人次就只能回 base COGS，無法出 BPG
    base_bpg = None
    if month_covers and float(month_covers) > 0:
        base_bpg = monthly["breakfast_cogs"] / float(month_covers)

    # 計算該日是否週末 & 月內週末/平日數量
    dt = datetime.strptime(date, "%Y-%m-%d").date()
    y, m = dt.year, dt.month
    days_in_month = calendar.monthrange(y, m)[1]
    weekend_days = sum(1 for d in range(1, days_in_month + 1)
                       if date_cls(y, m, d).weekday() >= 5)
    weekday_days = days_in_month - weekend_days
    is_weekend = dt.weekday() >= 5

    # 係數（先給原始加成，接著做「月平均=1」的歸一化）
    mult_weekend = 1.0 + float(weekend_uplift)
    mult_weekday = 1.0 + float(weekday_uplift)
    avg_mult = ((weekend_days * mult_weekend) + (weekday_days * mult_weekday)) / float(days_in_month) if days_in_month else 1.0
    day_mult = (mult_weekend / avg_mult) if is_weekend else (mult_weekday / avg_mult)

    bpg_est = base_bpg * day_mult if base_bpg is not None else None

    return JSONResponse({
        "date": date,
        "period": period,
        "covers_day": covers_day,
        "covers_month": month_covers,
        "base_bpg": base_bpg,          # 月基準 BPG
        "bpg_est": bpg_est,            # 當日推估 BPG
        "multiplier": day_mult,        # 當日係數（已歸一化）
        "is_weekend": is_weekend,
        "assumption": {
            "weekend_uplift": weekend_uplift,
            "weekday_uplift": weekday_uplift
        }
    })

# 讀每日 covers：data/covers/daily/YYYY-MM.csv 或 daily.csv
# 讀「當日」人次（支援 YYYY-MM-DD / YYYY/M/D）
def _try_load_daily_covers(iso_date: str) -> int | None:
    from datetime import datetime
    import csv

    def _norm(s: str) -> str:
        s = (s or "").strip().replace("/", "-").replace(".", "-")
        try:
            y, m, d = [int(x) for x in s.split("-")]
            return f"{y:04d}-{m:02d}-{d:02d}"
        except Exception:
            return s

    target = _norm(iso_date)
    y_m = target[:7]
    daily_dir = DATA_DIR.parent / "covers" / "daily"

    for p in [daily_dir / f"{y_m}.csv", daily_dir / "daily.csv"]:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        if _norm(row.get("date")) == target:
                            v = (row.get("covers") or "").replace(",", "").strip()
                            return int(float(v)) if v else None
            except Exception:
                continue
    return None


# 用每日 CSV 加總「本月」人次（找 2025-09.csv，否則從 daily.csv 篩 2025-09-*）
def _try_load_month_covers_from_daily(period: str) -> int | None:
    import csv
    daily_dir = DATA_DIR.parent / "covers" / "daily"

    def _norm(s: str) -> str:
        s = (s or "").strip().replace("/", "-").replace(".", "-")
        parts = s.split("-")
        if len(parts) == 3:
            try:
                y, m, d = [int(x) for x in parts]
                return f"{y:04d}-{m:02d}-{d:02d}"
            except Exception:
                pass
        return s

    for p in [daily_dir / f"{period}.csv", daily_dir / "daily.csv"]:
        if p.exists():
            total = 0
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        d = _norm(row.get("date"))
                        if d.startswith(period):
                            v = (row.get("covers") or "").replace(",", "").strip()
                            if v:
                                total += int(float(v))
                return total if total > 0 else None
            except Exception:
                continue
    return None

