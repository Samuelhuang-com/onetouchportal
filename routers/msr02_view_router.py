# routers/msr02_view_router.py
from fastapi import APIRouter, Request, Cookie
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
    FileResponse,
    JSONResponse,
)
from typing import Optional, List, Tuple
import pandas as pd
import os
import io
import json
import re
import sqlite3
from datetime import datetime

from app_utils import templates, get_base_context  # 沿用你專案的共用方法
from app_utils import ensure_permission_pack

ensure_permission_pack(
    "roomblock"
)  # 產生 roomblock_view / roomblock_manage / roomblock_admin

router = APIRouter(tags=["Revenue"])

# -----------------------------
# 路徑候選（Excel / DB）
# -----------------------------
EXCEL_CANDIDATES = [
    r"data/rv/msr02.xls",
    r"data/RV/MSR02.xlsx",
    r"data/MSR02.xlsx",
    r"MSR02.xlsx",
]

# MSR02 匯入規則：自 A7 起讀取，遇到 A 欄為「平均住房率：」停止
MSR02_START_CELL_ROW = 7
MSR02_STOP_TOKEN = "平均住房率："
# MSR02 欄位名稱（固定不變、照你提供的順序與名稱）
MSR02_DATA_COLUMNS = [
    "col",
    "Unnamed_1",
    "col_1",
    "col_2",
    "col_3",
    "col_4",
    "col_5",
    "col_6",
    "col_7",
    "col_8",
    "OOO",
    "MBK",
    "HUS",
    "COMP",
    "col_9",
    "col_10",
    "col_11",
    "FIT",
    "GIT",
    "col_12",
    "col_13",
    "AS_3",
    "SS_30",
    "SS_V_10",
    "ST_23",
    "ST_V_10",
    "DS_21",
    "DS_V_10",
    "DH_14",
    "DT_26",
    "DT_V_15",
    "ES_7",
    "ES_V_8",
    "EH_32",
    "EH_V_13",
    "JS_2",
    "US_6",
    "IS_1",
    "STP_1",
]
MSR02_META_COLUMNS = ["_ingested_at", "_source_sheet"]
MSR02_ALL_COLUMNS = MSR02_DATA_COLUMNS + MSR02_META_COLUMNS

DB_CANDIDATES = [
    r"data/portal.db",
    r"portal.db",
]

# -----------------------------
# RVR17（Room Block Report）路徑 & 目標 DB
# -----------------------------
RVR_CANDIDATES = [
    r"data/RV/RVR17.TXT",
    r"data/RV/RVR17.txt",
    r"RVR17.TXT",
    r"RVR17.txt",
]

EIS_DB_CANDIDATES = [
    r"data/portal.db",
    r"portal.db",
]


def _pick_excel_path() -> Optional[str]:
    for p in EXCEL_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _pick_db_path() -> str:
    """
    依序尋找可用的 DB 路徑；若皆不存在且包含資料夾的路徑，會自動建立資料夾。
    """
    for p in DB_CANDIDATES:
        d = os.path.dirname(p)
        if d and not os.path.exists(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        return p
    return "portal.db"


def _pick_eis_db_path() -> str:
    for p in EIS_DB_CANDIDATES:
        d = os.path.dirname(p)
        if d and not os.path.exists(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass
        return p
    return "eis.db"


def _pick_rvr_path() -> Optional[str]:
    for p in RVR_CANDIDATES:
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
    df = pd.read_excel(path, sheet_name=sheet_name, dtype=str)
    df = df.fillna("")
    total = len(df)
    if limit and limit > 0:
        df = df.head(limit)
    cols = [str(c) for c in df.columns]
    rows = df.to_dict(orient="records")
    return cols, rows, total


def _fetch_msr02_from_db(
    db_path: str,
    table: str = "MSR02",
    limit: Optional[int] = 300,
) -> Tuple[List[str], List[dict], int]:
    """
    直接從 SQLite 讀取 MSR02：依 schema 順序取欄名，撈資料並回傳 columns / records / total_rows
    """
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row

        # 取欄位順序（照資料表定義）
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            return [], [], 0
        # sqlite3.Row or tuple 都相容
        cols = [row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in info]
        col_list = ",".join([f'"{c}"' for c in cols])

        # 總筆數
        row_cnt = conn.execute(f'SELECT COUNT(1) AS c FROM "{table}"').fetchone()
        total_rows = int(
            row_cnt["c"] if isinstance(row_cnt, sqlite3.Row) else row_cnt[0]
        )

        # 資料
        if limit and int(limit) > 0:
            cur = conn.execute(
                f'SELECT {col_list} FROM "{table}" LIMIT ?', (int(limit),)
            )
        else:
            cur = conn.execute(f'SELECT {col_list} FROM "{table}"')
        records = [dict(r) for r in cur.fetchall()]

    return cols, records, total_rows


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


# -----------------------------
# 工具：欄位名稱清理 & 建表/寫入（MSR02）
# -----------------------------
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]+")


def _sanitize_col(col: str) -> str:
    name = str(col).strip().replace("\n", "_").replace("\r", "_")
    name = _SANITIZE_RE.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "col"
    if not re.match(r"^[A-Za-z_]", name):
        name = "col_" + name
    return name


def _create_table_sql(table: str, columns: List[str]) -> str:
    cols_sql = ", ".join([f'"{c}" TEXT' for c in columns])
    meta_sql = ', "_ingested_at" TEXT, "_source_sheet" TEXT'
    return f'CREATE TABLE IF NOT EXISTS "{table}" ({cols_sql}{meta_sql});'


def _insert_many(
    conn: sqlite3.Connection, table: str, columns: List[str], rows: List[tuple]
):
    placeholders = ", ".join(["?"] * (len(columns) + 2))  # +2 for meta
    cols_list_sql = ", ".join(
        [f'"{c}"' for c in columns] + ['"_ingested_at"', '"_source_sheet"']
    )
    sql = f'INSERT INTO "{table}" ({cols_list_sql}) VALUES ({placeholders});'
    conn.executemany(sql, rows)


def _excel_to_db(
    excel_path: str,
    db_path: str,
    table: str = "MSR02",
    sheet_name: Optional[str] = None,
    mode: str = "replace",  # replace | append
) -> dict:
    xl = pd.ExcelFile(excel_path)
    if not sheet_name or sheet_name not in xl.sheet_names:
        sheet_name = xl.sheet_names[0]

    # 讀取 Excel：不使用標頭（header=None），後續手動套用固定欄名
    df = pd.read_excel(
        excel_path, sheet_name=sheet_name, dtype=str, header=None
    ).fillna("")

    # === 由 A7 開始（Excel 列是 1-based；header=None 時，A1 對應 index=0）===
    start_idx = max(0, MSR02_START_CELL_ROW - 1)  # 7 -> index 6
    df = df.iloc[start_idx:].reset_index(drop=True)

    # === A 欄遇到「平均住房率：」即停止 ===
    a_col = df.iloc[:, 0].astype(str).str.strip().str.replace(":", "：", regex=False)
    stop_pos = a_col[a_col == MSR02_STOP_TOKEN].index.tolist()
    if stop_pos:
        df = df.iloc[: stop_pos[0]].copy()

    # 若沒有資料，直接收尾
    if df.empty:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            if mode == "replace":
                cur.execute(f'DROP TABLE IF EXISTS "{table}";')
            # 建表（固定欄位）
            cols_sql = ",\n    ".join([f'"{c}" TEXT' for c in MSR02_ALL_COLUMNS])
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (\n    {cols_sql}\n);')
            conn.commit()
        return {
            "db_path": db_path,
            "table": table,
            "sheet": sheet_name,
            "mode": mode,
            "rows_written": 0,
            "columns": MSR02_ALL_COLUMNS,
            "ingested_at": None,
        }

    # === 只保留固定欄數；多的丟棄，不足的補空字串 ===
    # 先裁到固定欄數
    df = df.iloc[:, : len(MSR02_DATA_COLUMNS)].copy()
    # 若不足，補上空白欄位
    if df.shape[1] < len(MSR02_DATA_COLUMNS):
        for _ in range(len(MSR02_DATA_COLUMNS) - df.shape[1]):
            df[df.shape[1]] = ""

    # 套用「固定欄位名稱」
    df.columns = MSR02_DATA_COLUMNS

    # 準備寫入資料
    ingested_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    data_rows = []
    for _, row in df.iterrows():
        vals = [str(row[c]) if pd.notna(row[c]) else "" for c in MSR02_DATA_COLUMNS]
        vals += [ingested_at, sheet_name]  # 兩個 meta 欄位
        data_rows.append(tuple(vals))

    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()

        # 依模式處理資料表
        if mode == "replace":
            cur.execute(f'DROP TABLE IF EXISTS "{table}";')

        # 建表（固定欄位結構，完全符合你提供的 DDL）
        cols_sql = ",\n    ".join([f'"{c}" TEXT' for c in MSR02_ALL_COLUMNS])
        cur.execute(f'CREATE TABLE IF NOT EXISTS "{table}" (\n    {cols_sql}\n);')

        # 寫入
        if data_rows:
            placeholders = ",".join(["?"] * len(MSR02_ALL_COLUMNS))
            col_list = ",".join([f'"{c}"' for c in MSR02_ALL_COLUMNS])
            cur.executemany(
                f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                data_rows,
            )
        conn.commit()

    return {
        "db_path": db_path,
        "table": table,
        "sheet": sheet_name,
        "mode": mode,
        "rows_written": len(data_rows),
        "columns": MSR02_ALL_COLUMNS,
        "ingested_at": ingested_at,
    }


# -----------------------------
# 頁面：檢視 MSR02（原有）
# -----------------------------
@router.get("/msr02", response_class=HTMLResponse)
async def msr02_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    show_all: Optional[int] = 0,
    limit: Optional[int] = 300,
):
    if not user:
        return RedirectResponse(url="/login")
    if not _has_report_permission(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    error = None
    columns: List[str] = []
    records: List[dict] = []
    total_rows = 0

    db_path = _pick_db_path()  # data/portal.db → portal.db（既有邏輯）

    try:
        effective_limit = None if show_all == 1 else (limit or 300)
        columns, records, total_rows = _fetch_msr02_from_db(
            db_path=db_path, table="MSR02", limit=effective_limit
        )
        if not columns:
            error = '在 DB 中找不到表 "MSR02" 或表無欄位/資料。'
    except Exception as e:
        error = f"讀取 portal.db/MSR02 發生錯誤：{e}"

    ctx = get_base_context(request, user, role, permissions)
    ctx.update(
        {
            "columns": columns,
            "records": records,
            "total_rows": total_rows,
            "show_all": show_all,
            "limit": limit or 300,
            "error": error,
        }
    )
    return templates.TemplateResponse("msr02_view.html", ctx)


# -----------------------------
# 下載 CSV（原有）
# -----------------------------
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


# -----------------------------
# 下載 Excel 原檔（原有）
# -----------------------------
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
    return FileResponse(excel_path, filename=os.path.basename(excel_path))


# -----------------------------
# 新增：將 Excel 寫入 DB（原有）
# -----------------------------
@router.post("/msr02/import")
async def import_msr02_to_db(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    sheet: Optional[str] = None,
    mode: Optional[str] = "replace",
    table: Optional[str] = "MSR02",
):
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse(
            {
                "ok": False,
                "error": "permission_denied",
                "message": "僅限管理員匯入資料",
            },
            status_code=403,
        )

    excel_path = _pick_excel_path()
    if not excel_path:
        return JSONResponse(
            {"ok": False, "error": "not_found", "message": "找不到 MSR02.xlsx"},
            status_code=404,
        )

    db_path = _pick_db_path()
    mode = (mode or "replace").lower()
    if mode not in ("replace", "append"):
        mode = "replace"

    try:
        result = _excel_to_db(
            excel_path=excel_path,
            db_path=db_path,
            table=table or "MSR02",
            sheet_name=sheet,
            mode=mode,
        )
        result.update({"ok": True})
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": "import_failed", "message": str(e)},
            status_code=500,
        )


# === 匯入頁面（原有，新增 rvr_path 顯示） ===
@router.get("/msr02/import-page", response_class=HTMLResponse)
async def msr02_import_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    sheet: Optional[str] = None,
):
    if not user:
        return RedirectResponse(url="/login")

    excel_path = _pick_excel_path()
    error = None
    sheet_names: List[str] = []
    selected_sheet = sheet

    if not excel_path:
        error = "找不到 MSR02.xlsx，請確認路徑是否存在（data/RV 或專案根目錄）。"
    else:
        sheet_names = _list_sheets(excel_path)
        if not sheet_names:
            error = "MSR02.xlsx 讀取失敗或沒有任何工作表。"
        else:
            if (not selected_sheet) or (selected_sheet not in sheet_names):
                selected_sheet = sheet_names[0]

    ctx = get_base_context(request, user, role, permissions)
    ctx.update(
        {
            "excel_path": excel_path,
            "sheet_names": sheet_names,
            "selected_sheet": selected_sheet,
            "error": error,
            # 新增：RVR17 現況顯示
            "rvr_path": _pick_rvr_path(),
        }
    )
    return templates.TemplateResponse("msr02_import.html", ctx)


# ---------------------------------------------------------------------
# （新增）Room Block Report：解析 RVR17.TXT 並匯入 data/eis.db 的 roomblock
# ---------------------------------------------------------------------

# 解析：固定欄位，使用正則避免中文字寬度導致的切割誤差
_RVR_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+(.+?)\s{2,}(.+?)\s+(\d{8})\s+(\d+)\s+(\S+)\s+(\d+/\d+)\s+(\S+)\s+(\S+)\s*(.*)$"
)
_RVR_COLS = [
    "序號",
    "訂房號碼",
    "訂房名稱",
    "合約公司",
    "到達日期",
    "夜次",
    "訂房類別",
    "人數/兒童",
    "房號",
    "房務狀況",
    "備註",
]


def _read_text_lines(path: str) -> List[str]:
    # 嘗試 Big5/CP950，再退回 UTF-8
    for enc in ("cp950", "big5", "utf-8"):
        try:
            with open(path, "r", encoding=enc, errors="ignore") as f:
                return [ln.rstrip("\r\n") for ln in f.readlines()]
        except Exception:
            continue
    return []


def _parse_rvr17(path: str) -> List[dict]:
    lines = _read_text_lines(path)
    rows = []
    for ln in lines:
        # 以「行首為數字」過濾資料列；略過標頭/分隔線/頁註
        s = ln.strip()
        if not s or not re.match(r"^\d{1,4}\s", s):
            continue
        m = _RVR_ROW_RE.match(ln)
        if not m:
            continue
        g = m.groups()
        rec = {_RVR_COLS[i]: g[i] for i in range(len(_RVR_COLS))}
        rows.append(rec)
    return rows


def _ensure_roomblock_table(conn: sqlite3.Connection):
    # 建立固定 schema（中文欄名需用雙引號）。全 TEXT，保留原始樣貌 + meta 欄位
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS "roomblock" (
            "序號"       TEXT,
            "訂房號碼"   TEXT,
            "訂房名稱"   TEXT,
            "合約公司"   TEXT,
            "到達日期"   TEXT,
            "夜次"       TEXT,
            "訂房類別"   TEXT,
            "人數/兒童"  TEXT,
            "房號"       TEXT,
            "房務狀況"   TEXT,
            "備註"       TEXT,
            "_ingested_at" TEXT,
            "_source_file" TEXT
        );
        """
    )


def _insert_roomblock(
    conn: sqlite3.Connection, rows: List[dict], mode: str, source_file: str
):
    cur = conn.cursor()
    if mode == "replace":
        cur.execute('DROP TABLE IF EXISTS "roomblock";')
    _ensure_roomblock_table(conn)

    ing = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    cols_sql = ", ".join(
        [f'"{c}"' for c in _RVR_COLS] + ['"_ingested_at"', '"_source_file"']
    )
    placeholders = ", ".join(["?"] * (len(_RVR_COLS) + 2))
    sql = f'INSERT INTO "roomblock" ({cols_sql}) VALUES ({placeholders});'

    data = []
    for r in rows:
        vals = [r.get(c, "") for c in _RVR_COLS] + [ing, os.path.basename(source_file)]
        data.append(tuple(vals))
    if data:
        cur.executemany(sql, data)
    conn.commit()
    return len(data), ing


@router.post("/roomblock/import")
async def import_roomblock(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    mode: Optional[str] = "replace",  # replace | append
):
    """
    解析 data/RV/RVR17.TXT，將 Room Block 報表寫入 data/eis.db 的 roomblock 表。
    欄位：序號、訂房號碼、訂房名稱、合約公司、到達日期、夜次、訂房類別、人數/兒童、房號、房務狀況、備註。
    僅管理員可執行；支援覆寫/追加兩種模式。
    """
    if not user:
        return RedirectResponse(url="/login")
    if role != "admin":
        return JSONResponse(
            {
                "ok": False,
                "error": "permission_denied",
                "message": "僅限管理員匯入資料",
            },
            status_code=403,
        )

    rvr_path = _pick_rvr_path()
    if not rvr_path:
        return JSONResponse(
            {"ok": False, "error": "not_found", "message": "找不到 data/RV/RVR17.TXT"},
            status_code=404,
        )

    try:
        rows = _parse_rvr17(rvr_path)
        db_path = _pick_db_path()
        mode = (mode or "replace").lower()
        if mode not in ("replace", "append"):
            mode = "replace"

        if not rows:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "no_rows",
                    "message": "RVR17 解析結果為空，請檢查檔案內容／編碼。",
                },
                status_code=400,
            )

        # 寫入
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            written, ing = _insert_roomblock(conn, rows, mode, rvr_path)

        # 回報到達日期範圍（方便快速確認）
        dates = [r.get("到達日期") for r in rows if r.get("到達日期")]
        dmin = min(dates) if dates else None
        dmax = max(dates) if dates else None

        return JSONResponse(
            {
                "ok": True,
                "db_path": db_path,
                "table": "roomblock",
                "rows_written": written,
                "ingested_at": ing,
                "file_path": rvr_path,
                "date_min": dmin,
                "date_max": dmax,
            }
        )
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": "import_failed", "message": str(e)},
            status_code=500,
        )


# ============================
# Room Block：檢視與下載 CSV
# ============================


def _fetch_roomblock(
    db_path: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 20000,
) -> Tuple[list, list, int]:
    """
    從 eis.db 讀取 roomblock 表；僅回傳 11 個指定欄位，並支援到達日期區間與關鍵字查詢。
    關鍵字針對：訂房號碼 / 訂房名稱 / 合約公司 / 房號。
    """
    cols = _RVR_COLS[:]  # 固定欄順序
    where = []
    params: list = []

    # 日期字串為 YYYYMMDD，TEXT 比對可直接區間比較
    if date_from:
        where.append('"到達日期" >= ?')
        params.append(date_from.strip())
    if date_to:
        where.append('"到達日期" <= ?')
        params.append(date_to.strip())

    if q:
        kw = f"%{q.strip()}%"
        where.append(
            '(("訂房號碼" LIKE ?) OR ("訂房名稱" LIKE ?) OR ("合約公司" LIKE ?) OR ("房號" LIKE ?))'
        )
        params.extend([kw, kw, kw, kw])

    wh_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
      SELECT "序號","訂房號碼","訂房名稱","合約公司","到達日期","夜次","訂房類別","人數/兒童","房號","房務狀況","備註"
      FROM "roomblock"
      {wh_sql}
      ORDER BY "到達日期","序號"
      LIMIT ?
    """
    params.append(int(limit))

    records = []
    total_rows = 0
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # 先求總筆數（給頁面提示）
        cur_cnt = conn.execute(
            f'SELECT COUNT(1) AS c FROM "roomblock"{wh_sql}', params[:-1]
        )
        row_cnt = cur_cnt.fetchone()
        total_rows = int(row_cnt["c"]) if row_cnt and row_cnt["c"] is not None else 0

        # 撈資料
        cur = conn.execute(sql, params)
        for r in cur.fetchall():
            rec = {c: (r[c] if c in r.keys() else "") for c in cols}
            records.append(rec)

    return cols, records, total_rows


@router.get("/roomblock", response_class=HTMLResponse)
async def roomblock_view(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
):
    """
    Room Block 檢視頁（比照 msr02_view 的表格風格與分頁文案）
    """
    if not user:
        return RedirectResponse(url="/login")
    if not _has_report_permission(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    db_path = _pick_eis_db_path()
    if not os.path.exists(db_path):
        ctx = get_base_context(request, user, role, permissions)
        ctx.update(
            {
                "error": "找不到 eis.db，請先執行匯入（msr02_import 頁下方的 Room Block 匯入工具）。",
                "columns": [],
                "records": [],
                "total_rows": 0,
                "date_from": date_from,
                "date_to": date_to,
                "q": q,
            }
        )
        return templates.TemplateResponse("roomblock_view.html", ctx)

    # 確認表存在
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute('SELECT 1 FROM "roomblock" LIMIT 1;')
    except Exception:
        ctx = get_base_context(request, user, role, permissions)
        ctx.update(
            {
                "error": "尚未建立 roomblock 資料表，請先匯入 RVR17.TXT。",
                "columns": [],
                "records": [],
                "total_rows": 0,
                "date_from": date_from,
                "date_to": date_to,
                "q": q,
            }
        )
        return templates.TemplateResponse("roomblock_view.html", ctx)

    # 讀取資料
    columns, records, total_rows = _fetch_roomblock(
        db_path, date_from=date_from, date_to=date_to, q=q, limit=20000
    )

    ctx = get_base_context(request, user, role, permissions)
    ctx.update(
        {
            "columns": columns,
            "records": records,
            "total_rows": total_rows,
            "date_from": date_from,
            "date_to": date_to,
            "q": q,
        }
    )
    return templates.TemplateResponse("roomblock_view.html", ctx)


@router.get("/roomblock/download/csv")
async def roomblock_download_csv(
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    q: Optional[str] = None,
):
    """
    匯出目前篩選條件的 Room Block CSV（UTF-8 BOM，方便 Excel）
    """
    if not user:
        return RedirectResponse(url="/login")
    if not _has_report_permission(role, permissions):
        return RedirectResponse(url="/dashboard?error=permission_denied")

    db_path = _pick_eis_db_path()
    if not os.path.exists(db_path):
        return HTMLResponse("找不到 eis.db", status_code=404)

    try:
        columns, records, _ = _fetch_roomblock(
            db_path, date_from, date_to, q, limit=200000
        )
    except Exception as e:
        return HTMLResponse(f"查詢失敗：{e}", status_code=500)

    df = pd.DataFrame.from_records(records, columns=columns)
    stream = io.StringIO()
    df.to_csv(stream, index=False, encoding="utf-8-sig")
    stream.seek(0)

    # 用條件組檔名
    def safe(v):
        return (v or "").strip()

    fn = f"RoomBlock_{safe(date_from) or 'all'}_{safe(date_to) or 'all'}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{fn}"'}
    return StreamingResponse(
        iter([stream.getvalue()]), media_type="text/csv", headers=headers
    )
