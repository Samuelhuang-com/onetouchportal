# routers/eis_router.py
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict
from urllib.parse import quote_plus

from fastapi import APIRouter, Request, Depends, Cookie, Query, Form
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse

# 你專案裡原本就有的共用
from app_utils import templates, get_base_context, check_permission
from services.eis_ingest import import_for_date, DB_PATH

TZ = ZoneInfo("Asia/Taipei")
router = APIRouter(prefix="/eis", tags=["Revenue"])

EIS_COLUMNS: List[str] = ["日期", "部門", "項目", "本日金額"]


def _parse_date(s: Optional[str], default: Optional[date]) -> date:
    if not s:
        return default
    return datetime.strptime(s, "%Y-%m-%d").date()


def _query_records(s: date, e: date) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT report_date, department, item, amount
                FROM EISDB
                WHERE department not in('(DUS)','(含DUS)','(不含DUS)') AND report_date BETWEEN ? AND ?
                ORDER BY report_date ASC, department ASC, item ASC
            """,
                (s.isoformat(), e.isoformat()),
            )
            for rd, dept, item, amt in cur.fetchall():
                rows.append(
                    {
                        "日期": rd,
                        "部門": str(dept or ""),
                        "項目": str(item or ""),
                        "本日金額": f"{float(amt):.0f}",
                    }
                )
    return rows


# ✅ 新增：把明細一起查出來，提供 rooms / persons
def _query_detail_records(s: date, e: date) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT report_date, type, item, Amount, rooms, persons
                FROM EISDetailDB
                WHERE report_date BETWEEN ? AND ?
                ORDER BY report_date ASC, type ASC, item ASC
            """,
                (s.isoformat(), e.isoformat()),
            )
            for rd, t, item, amount, rooms, persons in cur.fetchall():
                rows.append(
                    {
                        "report_date": rd,
                        "type": t or "",
                        "item": item or "",
                        "amount": float(amount) if amount is not None else 0.0,
                        "rooms": int(rooms) if rooms is not None else 0,
                        "persons": int(persons) if persons is not None else 0,
                    }
                )
    return rows


def _query_min_max_date():
    min_d = max_d = None
    if DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            # 主表
            cur.execute("SELECT MIN(report_date), MAX(report_date) FROM EISDB")
            m1, M1 = cur.fetchone() or (None, None)
            # 明細表
            cur.execute("SELECT MIN(report_date), MAX(report_date) FROM EISDetailDB")
            m2, M2 = cur.fetchone() or (None, None)

            # 取兩張表的聯集範圍
            candidates_min = [d for d in [m1, m2] if d]
            candidates_max = [d for d in [M1, M2] if d]
            if candidates_min:
                min_d = min(candidates_min)
            if candidates_max:
                max_d = max(candidates_max)
    return min_d, max_d


@router.get("", response_class=HTMLResponse, name="eis_index")
async def eis_index(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_view: bool = Depends(check_permission("eis_view")),
    can_import: bool = Depends(check_permission("eis_import")),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not has_view:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    today = datetime.now(TZ).date()
    s = _parse_date(start, today - timedelta(days=6))
    e = _parse_date(end, today)

    records = _query_records(s, e)
    detail_rows = _query_detail_records(s, e)

    ctx = get_base_context(request, user, role, permissions)
    min_d, max_d = _query_min_max_date()
    
    ctx.update(
        {
            "columns": EIS_COLUMNS,
            "records": records,
            "detail_rows": detail_rows,  # ← 給模板的明細
            "total_rows": len(records),
            "start": s,
            "end": e,
            "yesterday": today - timedelta(days=1),
            "can_import": can_import,
            "error": None,
            "min_date": min_d,
            "max_date": max_d,
        }
    )
    return templates.TemplateResponse("eis/index.html", ctx)


@router.post("/import-by-date", name="eis_import_by_date")
async def eis_import_by_date(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_import: bool = Depends(check_permission("eis.import")),
    import_date: str = Form(...),
):
    if not user:
        return RedirectResponse(url="/login")
    if not has_import:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    try:
        target = datetime.strptime(import_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        return RedirectResponse(
            url=f"/eis?error=PY_DEP&msg={quote_plus('import_date 格式錯誤')}",
            status_code=303,
        )

    try:
        # ✅ 同步匯入主表＋明細，並回傳兩個筆數
        main_count, detail_count = import_for_date(target)
        return RedirectResponse(
            url=f"/eis?start={target:%Y-%m-%d}&end={target:%Y-%m-%d}&imported={main_count}&detail={detail_count}",
            status_code=303,
        )
    except FileNotFoundError as e:
        return RedirectResponse(
            url=f"/eis?error=FILE_NOT_FOUND&msg={quote_plus(str(e))}", status_code=303
        )
    except ImportError as e:
        return RedirectResponse(
            url=f"/eis?error=PY_DEP&msg={quote_plus(str(e))}", status_code=303
        )
    except RuntimeError as e:
        # 若 parse detail 失敗時拋出的訊息
        return RedirectResponse(
            url=f"/eis?error=DETAIL_PARSE&msg={quote_plus(str(e))}", status_code=303
        )


@router.get("/download/csv", name="eis_download_csv")
async def eis_download_csv(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_view: bool = Depends(check_permission("eis_view")),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    if not user:
        return RedirectResponse(url="/login")
    if not has_view:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    today = datetime.now(TZ).date()
    s = _parse_date(start, today - timedelta(days=6))
    e = _parse_date(end, today)

    records = _query_records(s, e)

    import io, csv

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EIS_COLUMNS)
    for r in records:
        writer.writerow([r[c] for c in EIS_COLUMNS])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="EIS_{s:%Y%m%d}_{e:%Y%m%d}.csv"'
        },
    )
