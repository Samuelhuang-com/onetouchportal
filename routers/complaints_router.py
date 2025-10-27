# routers/complaints_router.py  —— 完整覆蓋版
from fastapi import APIRouter, Request, Form, UploadFile, File, Cookie, HTTPException
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Generator
from datetime import datetime, date, time, timedelta
import sqlite3, os, uuid, shutil, re, json, asyncio

from app_utils import templates, get_base_context

router = APIRouter(prefix="/complaints", tags=["complaints"])
UPLOAD_DIR = Path("data") / "complaints_files"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---- DB Access ----
try:
    from data.db import get_conn  # 你專案既有
except Exception:
    DB_PATH = Path("data") / "app.db"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    def get_conn():
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


# ---- Constants (保留原邏輯) ----
STATUSES = ["開啟", "處理中", "待回覆", "已完成", "已轉外部"]
SEVERITIES = ["低", "中", "高", "緊急"]
HEADERS = [
    "ComplaintID",
    "Date",
    "Time",
    "Room",
    "GuestName",
    "Phone",
    "BookingSource",
    "Category",
    "Severity",
    "Title",
    "Description",
    "Dept",
    "Assignee",
    "Status",
    "Resolution",
    "CreatedAt",
    "UpdatedAt",
]

# 內建關鍵字（當 DB 沒規則時 fallback 使用）
SEED_KEYWORDS: Dict[str, List[str]] = {
    "網路/Wi-Fi": [
        r"網路",
        r"wifi",
        r"wi[-\s]*fi",
        r"internet",
        r"寬頻",
        r"斷線",
        r"連不上",
        r"網速",
    ],
    "冷氣/空調": [
        r"冷氣",
        r"空調",
        r"\bAC\b",
        r"air ?con",
        r"太冷",
        r"太熱",
        r"不冷",
        r"不熱",
        r"溫度",
        r"風量",
        r"壓縮機",
    ],
    "熱水/水壓/漏水": [
        r"熱水",
        r"沒熱水",
        r"水溫",
        r"水壓",
        r"漏水",
        r"滲水",
        r"滴水",
        r"排水",
        r"馬桶",
        r"堵",
        r"水管",
        r"淋浴",
    ],
    "電視/遙控器": [r"電視", r"\bTV\b", r"遙控器", r"頻道", r"\bHDMI\b"],
    "電力/照明/插座": [
        r"停電",
        r"跳電",
        r"插座",
        r"充電",
        r"燈",
        r"電燈",
        r"照明",
        r"電力",
    ],
    "噪音/隔音": [r"噪音", r"吵", r"隔音", r"施工", r"敲", r"喧嘩"],
    "氣味/異味": [r"味", r"臭", r"煙味", r"霉味", r"異味", r"香水"],
    "清潔/衛生": [r"髒", r"灰塵", r"頭髮", r"清潔", r"衛生", r"黴", r"黴菌"],
    "服務/態度": [
        r"服務",
        r"態度",
        r"前台",
        r"櫃臺",
        r"櫃台",
        r"等待",
        r"慢",
        r"不禮貌",
        r"投訴人員",
    ],
    "帳單/付款": [r"帳單", r"收據", r"付款", r"發票", r"刷卡", r"退款", r"多收"],
    "早餐/餐飲": [r"早餐", r"餐廳", r"餐", r"食物", r"餐點", r"牛肉麵", r"餐具"],
    "電梯": [r"電梯", r"梯"],
    "床/寢具": [r"床", r"床墊", r"枕", r"被"],
    "備品/設施": [
        r"備品",
        r"牙刷",
        r"毛巾",
        r"瓶裝水",
        r"拖鞋",
        r"浴帽",
        r"吹風機",
        r"設施",
        r"健身房",
    ],
    "安全/門鎖/窗": [r"門鎖", r"門卡", r"門", r"窗", r"安全"],
    "停車": [r"停車", r"車位", r"停車場"],
}
ENGINEERING_SET = set(
    [
        "網路/Wi-Fi",
        "冷氣/空調",
        "熱水/水壓/漏水",
        "電視/遙控器",
        "電力/照明/插座",
        "電梯",
        "安全/門鎖/窗",
    ]
)

# ---- Schema ----
CREATE_COMPLAINTS = """
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ComplaintID TEXT UNIQUE,
    Date TEXT NOT NULL,
    Time TEXT NOT NULL,
    Room TEXT,
    GuestName TEXT,
    Phone TEXT,BookingSource TEXT,
    Category TEXT,
    Severity TEXT,
    Title TEXT,
    Description TEXT NOT NULL,
    Dept TEXT,
    Assignee TEXT,
    Status TEXT,
    Resolution TEXT,CreatedBy TEXT, 
    CreatedAt TEXT NOT NULL,
    UpdatedAt TEXT NOT NULL
);
"""
CREATE_FILES = """
CREATE TABLE IF NOT EXISTS complaint_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL,
    stored_name TEXT NOT NULL,
    orig_name   TEXT,
    mime        TEXT,
    size        INTEGER,
    created_at  TEXT NOT NULL,
    FOREIGN KEY(complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
);
"""
CREATE_IDX = [
    "CREATE INDEX IF NOT EXISTS idx_complaints_date ON complaints(Date)",
    "CREATE INDEX IF NOT EXISTS idx_complaints_status ON complaints(Status)",
]

# 規則與分類（新表）
CREATE_CATEGORY = """
CREATE TABLE IF NOT EXISTS complaint_category (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    is_engineering INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 100,
    updated_at TEXT NOT NULL
);
"""
CREATE_PATTERN = """
CREATE TABLE IF NOT EXISTS complaint_category_pattern (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(category_id) REFERENCES complaint_category(id) ON DELETE CASCADE
);
"""
# 每週告警（供 SSE 推播）
CREATE_ALERT = """
CREATE TABLE IF NOT EXISTS complaint_hot_alert (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,      -- ISO YYYY-MM-DD（每週一）
    room TEXT NOT NULL,
    category TEXT NOT NULL,
    count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(week_start, room, category)
);
"""

# 版本記錄（針對「處理/回覆」的歷程）
CREATE_RESOLUTION_LOGS = """
CREATE TABLE IF NOT EXISTS complaint_resolution_logs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL,
    dept         TEXT,
    author_name  TEXT,
    content      TEXT NOT NULL,
    created_by   TEXT,
    created_at   TEXT NOT NULL,
    FOREIGN KEY(complaint_id) REFERENCES complaints(id) ON DELETE CASCADE
);
"""
RESOLUTION_IDX = [
    "CREATE INDEX IF NOT EXISTS idx_resolution_logs_c ON complaint_resolution_logs(complaint_id)",
    "CREATE INDEX IF NOT EXISTS idx_resolution_logs_dt ON complaint_resolution_logs(created_at)",
]


def ensure_db():
    with get_conn() as c:
        c.execute(CREATE_COMPLAINTS)
        c.execute(CREATE_FILES)
        for sql in CREATE_IDX:
            c.execute(sql)

        c.execute(CREATE_CATEGORY)
        c.execute(CREATE_PATTERN)
        c.execute(CREATE_ALERT)

        # === 新增：處理/回覆版本記錄表與索引 ===
        c.execute(CREATE_RESOLUTION_LOGS)
        for sql in RESOLUTION_IDX:
            c.execute(sql)
        # === 新增段落結束 ===

        # 既有資料表安全補欄位（若已存在會進 except）
        try:
            c.execute("ALTER TABLE complaints ADD COLUMN BookingSource TEXT")
        except Exception:
            pass
        try:
            c.execute("ALTER TABLE complaints ADD COLUMN CreatedBy TEXT")
        except Exception:
            pass

    ensure_rules_seed()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_rules_seed():
    """第一次啟動時，若 DB 無任何分類，就把 SEED_KEYWORDS 寫入 DB。"""
    with get_conn() as c:
        n = c.execute("SELECT COUNT(1) FROM complaint_category").fetchone()[0]
        if (n or 0) > 0:
            return
        now = _now_str()
        for idx, (name, pats) in enumerate(SEED_KEYWORDS.items(), start=1):
            is_eng = 1 if name in ENGINEERING_SET else 0
            c.execute(
                "INSERT INTO complaint_category (name, is_engineering, active, sort_order, updated_at) VALUES (?,?,?,?,?)",
                (name, is_eng, 1, idx, now),
            )
            cat_id = c.execute(
                "SELECT id FROM complaint_category WHERE name=?", (name,)
            ).fetchone()["id"]
            for p in pats:
                c.execute(
                    "INSERT INTO complaint_category_pattern (category_id, pattern, updated_at) VALUES (?,?,?)",
                    (cat_id, p, now),
                )


# ---- Helpers ----
def _to_text(v) -> str:
    return "" if v is None else str(v).strip()


def _is_admin(role: Optional[str], permissions: Optional[str]) -> bool:
    r = (role or "").lower()
    pset = {p.strip().lower() for p in (permissions or "").split(",") if p.strip()}
    return r == "admin" or "admin" in pset


# 放在 _can_edit 旁邊
def _can_manage(role: Optional[str], permissions: Optional[str]) -> bool:
    # 管理權限：admin、role=manager / manage、或具管理型權限鍵
    if _is_admin(role, permissions):
        return True
    r = (role or "").lower()
    if r in ("manager", "manage"):  # ← 新增支援 role=manage
        return True
    pset = {p.strip().lower() for p in (permissions or "").split(",") if p.strip()}
    return (
        "complaints_manage" in pset
        or "complaints_manager" in pset
        or "permissions_manager" in pset
    )


def _can_edit(role: Optional[str], permissions: Optional[str]) -> bool:
    # 可編輯：admin、能 manage 的人、或具編輯型權限鍵
    if _is_admin(role, permissions) or _can_manage(role, permissions):
        return True
    pset = {p.strip().lower() for p in (permissions or "").split(",") if p.strip()}
    return ("complaints.edit" in pset) or ("callcenter.edit" in pset)


def _load_categories_from_db(active_only=True) -> List[sqlite3.Row]:
    with get_conn() as c:
        if active_only:
            rows = c.execute(
                "SELECT * FROM complaint_category WHERE active=1 ORDER BY sort_order, name"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM complaint_category ORDER BY sort_order, name"
            ).fetchall()
    return rows


def _load_patterns_map() -> Dict[str, List[str]]:
    """回傳 {category_name: [pattern1, ...]}"""
    with get_conn() as c:
        rows = c.execute(
            """
            SELECT c.name AS cname, p.pattern
            FROM complaint_category c
            JOIN complaint_category_pattern p ON p.category_id = c.id
            WHERE c.active=1
        """
        ).fetchall()
    out: Dict[str, List[str]] = {}
    for r in rows:
        out.setdefault(r["cname"], []).append(r["pattern"])
    return out


ROOM_DIGIT_RE = re.compile(r"(\d+)")


def normalize_room(x: str) -> str:
    s = _to_text(x)
    if not s:
        return ""
    m = ROOM_DIGIT_RE.search(s)
    return m.group(1) if m else s


def extract_floor(room_str: str) -> Optional[int]:
    s = _to_text(room_str)
    m = re.match(r"(\d{1,2})\d{2,3}$", s)
    if m:
        return int(m.group(1))
    m2 = re.search(r"(\d{1,2})", s)
    return int(m2.group(1)) if m2 else None


def parse_dt(date_text: str, time_text: str, created_at: str) -> Optional[datetime]:
    if _to_text(created_at):
        try:
            return datetime.fromisoformat(_to_text(created_at))
        except:
            pass
    d = _to_text(date_text)
    t = _to_text(time_text)
    if not d:
        return None
    try:
        return datetime.fromisoformat(f"{d} {t}") if t else datetime.fromisoformat(d)
    except:
        return None


def classify_text_by_db(title: str, desc: str) -> List[str]:
    """優先用 DB 規則；若 DB 無規則，fallback 到 SEED_KEYWORDS。"""
    txt = f"{_to_text(title)} {_to_text(desc)}".lower()
    if not txt.strip():
        return []
    patterns = _load_patterns_map()
    cats: List[str] = []
    if patterns:
        for cat, pats in patterns.items():
            for pat in pats:
                try:
                    if re.search(pat, txt, flags=re.IGNORECASE):
                        cats.append(cat)
                        break
                except re.error:
                    # 忽略不合法 regex
                    continue
    else:
        for cat, pats in SEED_KEYWORDS.items():
            for pat in pats:
                if re.search(pat, txt, flags=re.IGNORECASE):
                    cats.append(cat)
                    break
    # 去重保序
    seen = set()
    out = []
    for c in cats:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def _gen_id() -> str:
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"OT-CPL-{today}-"
    with get_conn() as c:
        n = c.execute(
            "SELECT COUNT(1) FROM complaints WHERE ComplaintID LIKE ?", (f"{prefix}%",)
        ).fetchone()[0]
    return prefix + str(int(n or 0) + 1).zfill(4)


# ---- Pages ----
@router.get("/")
def page(
    request: Request,
    user: Optional[str] = Cookie(default=None),
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    ensure_db()
    ctx = get_base_context(request, user, role, permissions)
    ctx["is_admin"] = _is_admin(role, permissions)
    ctx["can_edit"] = _can_edit(role, permissions)
    ctx["can_manage"] = _can_manage(role, permissions)

    return templates.TemplateResponse("complaints/complaints.html", ctx)


@router.get("/list")
def page_list(
    request: Request,
    user: Optional[str] = Cookie(default=None),
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    ensure_db()
    ctx = get_base_context(request, user, role, permissions)
    ctx["is_admin"] = _is_admin(role, permissions)
    ctx["can_manage"] = _can_manage(role, permissions)
    return templates.TemplateResponse("complaints/complaints_list.html", ctx)


# ---- APIs（原有介面保留） ----
@router.get("/api/init")
def api_init():
    ensure_db()
    # 既有：分類
    try:
        cats = _load_categories_from_db(active_only=True)
        categories = [r["name"] for r in cats] if cats else list(SEED_KEYWORDS.keys())
    except Exception:
        categories = list(SEED_KEYWORDS.keys())

    # === 新增：從 data/approvals.db 抓部門 + 對應人員 ===
    depts = []
    agents_map = {}
    try:
        adb = Path("data") / "approvals.db"
        if adb.exists():
            conn = sqlite3.connect(str(adb))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()

            # 取部門清單（department / department_1 去重、去空白）
            rows = c.execute(
                """
                SELECT DISTINCT TRIM(x) AS d
                FROM (
                    SELECT department AS x FROM employees
                    UNION ALL
                    SELECT department_1 AS x FROM employees
                )
                WHERE TRIM(COALESCE(x,'')) <> ''
                ORDER BY d
            """
            ).fetchall()
            depts = [r["d"] for r in rows]

            # 依部門取人員（英文名 + 中文名；去重）
            for d in depts:
                rs = c.execute(
                    """
                    SELECT COALESCE(english_name,'') AS en, COALESCE(name,'') AS nm
                    FROM employees
                    WHERE TRIM(COALESCE(department,'')) = TRIM(?)
                       OR TRIM(COALESCE(department_1,'')) = TRIM(?)
                """,
                    (d, d),
                ).fetchall()
                seen, lst = set(), []
                for r in rs:
                    label = (
                        f"{r['en']} {r['nm']}".strip()
                        if r["en"] and r["nm"]
                        else (r["en"] or r["nm"])
                    )
                    if label and label not in seen:
                        lst.append(label)
                        seen.add(label)
                agents_map[d] = lst

            conn.close()
    except Exception:
        # 任何讀取錯誤就交給前端預設（不讓頁面壞）
        pass

    # 若 approvals 還沒建好，避免前端空白：給一組安全預設
    if not depts:
        depts = [
            "櫃檯",
            "房務",
            "工程",
            "餐飲",
            "會計",
            "資訊",
            "行銷",
            "採購",
            "人資",
            "其他",
        ]

    return {
        "statuses": STATUSES,
        "severities": SEVERITIES,
        "headers": HEADERS,
        "categories": categories,
        "auto_categories": categories,
        "depts": depts,  # ← 前端 fillSelect($('#dept'), INIT.depts, …) 會用到
        "agentsByDept": agents_map,  # ← 前端 onChange 部門時依此帶出 assignee
    }


@router.get("/api/list")
def api_list(limit: int = 50, q: str = ""):
    ensure_db()
    limit = max(1, min(int(limit or 50), 500))
    base = f"SELECT {','.join(HEADERS)} FROM complaints"
    params: List = []
    if q := (q or "").strip():
        like = f"%{q}%"
        base += " WHERE " + " OR ".join([f"{col} LIKE ?" for col in HEADERS])
        params.extend([like] * len(HEADERS))
    base += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_conn() as c:
        rows = c.execute(base, params).fetchall()
    data = []
    for r in rows:
        item = {HEADERS[i]: r[i] for i in range(len(HEADERS))}
        item["Room"] = normalize_room(item.get("Room", ""))
        data.append(item)
    return data


@router.get("/api/list_paged")
def api_list_paged(q: str = "", page: int = 1, page_size: int = 50):
    ensure_db()
    try:
        with get_conn() as c:
            c.row_factory = sqlite3.Row
            # 想要回傳的欄位（含 CreatedBy / BookingSource）
            want_cols = [
                "ComplaintID",
                "Date",
                "Time",
                "Room",
                "GuestName",
                "Phone",
                "BookingSource",
                "Category",
                "Severity",
                "Title",
                "Description",
                "Dept",
                "Assignee",
                "Status",
                "Resolution",
                "CreatedAt",
                "UpdatedAt",
                "CreatedBy",
            ]
            # 僅選擇實際存在的欄位（避免舊庫尚未 ALTER 時發生 no such column）
            have = {r["name"] for r in c.execute("PRAGMA table_info(complaints)")}
            cols = [x for x in want_cols if x in have]
            where, params = "", []
            if q.strip():
                like = f"%{q.strip()}%"
                # 在現有欄位中做 OR 檢索
                where = " WHERE " + " OR ".join([f"{k} LIKE ?" for k in cols])
                params = [like] * len(cols)
            # total
            total = c.execute(
                f"SELECT COUNT(*) AS n FROM complaints{where}", params
            ).fetchone()["n"]
            pages = max((total + page_size - 1) // page_size, 1)
            page = max(min(page, pages), 1)
            off = (page - 1) * page_size
            # rows
            rows = c.execute(
                f"SELECT {','.join(cols)} FROM complaints{where} ORDER BY CreatedAt DESC LIMIT ? OFFSET ?",
                params + [page_size, off],
            ).fetchall()
            return {
                "ok": True,
                "rows": [dict(r) for r in rows],
                "total": total,
                "pages": pages,
                "page": page,
            }
    except Exception as e:
        return {"ok": False, "error": f"list_paged failed: {e}"}


# 區間查詢：輸出 PNG 用（回傳需要的欄位）
@router.get("/api/list_range")
def api_list_range(start: str, end: str, q: str = ""):
    ensure_db()
    try:
        with get_conn() as c:
            c.row_factory = sqlite3.Row
            want_cols = [
                "Date",
                "Time",
                "Room",
                "Title",
                "Description",
                "Dept",
                "Status",
                "Resolution",
                "CreatedBy",
                "BookingSource",  # BookingSource 仍在最後
            ]
            have = {r["name"] for r in c.execute("PRAGMA table_info(complaints)")}
            cols = [x for x in want_cols if x in have]
            where = " WHERE Date BETWEEN ? AND ?"
            params = [start, end]
            if q.strip():
                like = f"%{q.strip()}%"
                where += " AND (" + " OR ".join([f"{k} LIKE ?" for k in cols]) + ")"
                params += [like] * len(cols)
            rows = c.execute(
                f"SELECT {','.join(cols)} FROM complaints{where} ORDER BY Date, Time, id",
                params,
            ).fetchall()
            return {
                "ok": True,
                "start": start,
                "end": end,
                "rows": [dict(r) for r in rows],
            }
    except Exception as e:
        return {"ok": False, "error": f"list_range failed: {e}"}


# ---- Create / Update（原邏輯保留） ----
@router.post("/api/create")
async def api_create(
    date_: str = Form(...),
    time_: str = Form(...),
    room: str = Form(""),
    guest_name: str = Form(""),
    phone: str = Form(""),
    booking_source: str = Form(""),
    category: str = Form("其他"),
    severity: str = Form("中"),
    title: str = Form(""),
    description: str = Form(...),
    dept: str = Form(""),
    assignee: str = Form(""),
    status: str = Form("開啟"),
    resolution: str = Form(""),
    files: List[UploadFile] = File(default=[]),
    user: Optional[str] = Cookie(default=None),
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _can_edit(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    try:
        date.fromisoformat(date_)
    except:
        return JSONResponse({"ok": False, "error": "日期格式錯誤"}, 400)
    try:
        time.fromisoformat(time_)
    except:
        return JSONResponse({"ok": False, "error": "時間格式錯誤"}, 400)
    now = _now_str()
    comp_id = _gen_id()
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO complaints
            (ComplaintID,Date,Time,Room,GuestName,Phone,BookingSource,Category,Severity,Title,Description,Dept,Assignee,Status,Resolution,CreatedBy,CreatedAt,UpdatedAt)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                comp_id,
                date_,
                time_,
                room,
                guest_name,
                phone,
                booking_source,
                category,
                severity,
                title,
                description,
                dept,
                assignee,
                status,
                resolution,
                (user or ""),
                now,
                now,
            ),
        )

        cid = c.execute(
            "SELECT id FROM complaints WHERE ComplaintID=?", (comp_id,)
        ).fetchone()["id"]
        for f in files or []:
            if not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            uid = uuid.uuid4().hex + ext
            dest = UPLOAD_DIR / uid
            with dest.open("wb") as w:
                shutil.copyfileobj(f.file, w)
            c.execute(
                "INSERT INTO complaint_files (complaint_id,stored_name,orig_name,mime,size,created_at) VALUES (?,?,?,?,?,?)",
                (cid, uid, f.filename, f.content_type or "", dest.stat().st_size, now),
            )
    return {"ok": True, "ComplaintID": comp_id}


@router.post("/api/save/{complaint_id}")
async def api_save(
    complaint_id: str,
    date_: str = Form(...),
    time_: str = Form(...),
    room: str = Form(""),
    guest_name: str = Form(""),
    phone: str = Form(""),
    booking_source: str = Form(""),
    category: str = Form("其他"),
    severity: str = Form("中"),
    title: str = Form(""),
    description: str = Form(...),
    dept: str = Form(""),
    assignee: str = Form(""),
    status: str = Form("開啟"),
    resolution: str = Form(""),
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _can_edit(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    try:
        date.fromisoformat(date_)
    except:
        return JSONResponse({"ok": False, "error": "日期格式錯誤"}, 400)
    try:
        time.fromisoformat(time_)
    except:
        return JSONResponse({"ok": False, "error": "時間格式錯誤"}, 400)
    now = _now_str()
    with get_conn() as c:
        row = c.execute(
            "SELECT id FROM complaints WHERE ComplaintID=?", (complaint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        c.execute(
            """
            UPDATE complaints SET
                Date=?,Time=?,Room=?,GuestName=?,Phone=?,BookingSource=?,Category=?,Severity=?,Title=?,Description=?,
                Dept=?,Assignee=?,Status=?,Resolution=?,UpdatedAt=?
            WHERE ComplaintID=?
        """,
            (
                date_,
                time_,
                room,
                guest_name,
                phone,
                booking_source,  # ★ 新增值
                category,
                severity,
                title,
                description,
                dept,
                assignee,
                status,
                resolution,
                now,
                complaint_id,
            ),
        )
    return {"ok": True, "ComplaintID": complaint_id}


# ---- Files（原邏輯保留） ----
@router.post("/api/create_draft")
def api_create_draft(
    user: Optional[str] = Cookie(default=None),
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _can_edit(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    now = datetime.now()
    date_ = now.strftime("%Y-%m-%d")
    time_ = now.strftime("%H:%M")
    ts = _now_str()
    comp_id = _gen_id()
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO complaints
            (ComplaintID,Date,Time,Room,GuestName,Phone,BookingSource,Category,Severity,Title,Description,Dept,Assignee,Status,Resolution,CreatedBy,CreatedAt,UpdatedAt)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                comp_id,
                date_,
                time_,
                "",
                "",
                "",
                "",  # Room, GuestName, Phone, BookingSource
                "其他",
                "中",
                "（草稿）",
                "（草稿）",
                "",
                "",
                "開啟",
                "",
                (user or ""),
                ts,
                ts,
            ),
        )

    return {"ok": True, "ComplaintID": comp_id}


@router.post("/api/upload/{complaint_id}")
async def api_upload(
    complaint_id: str,
    files: List[UploadFile] = File(...),
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _can_edit(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    now = _now_str()
    with get_conn() as c:
        row = c.execute(
            "SELECT id FROM complaints WHERE ComplaintID=?", (complaint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        cid = row["id"]
        created = []
        for f in files or []:
            if not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            uid = uuid.uuid4().hex + ext
            dest = UPLOAD_DIR / uid
            with dest.open("wb") as w:
                shutil.copyfileobj(f.file, w)
            c.execute(
                "INSERT INTO complaint_files (complaint_id,stored_name,orig_name,mime,size,created_at) VALUES (?,?,?,?,?,?)",
                (cid, uid, f.filename, f.content_type or "", dest.stat().st_size, now),
            )
            new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            created.append({"id": new_id, "orig_name": f.filename})
    return {"ok": True, "files": created}


@router.post("/api/file/delete/{file_id}")
def api_file_delete(
    file_id: int,
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _is_admin(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT stored_name FROM complaint_files WHERE id=?", (file_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        path = UPLOAD_DIR / row["stored_name"]
        c.execute("DELETE FROM complaint_files WHERE id=?", (file_id,))
    try:
        if path.exists():
            path.unlink()
    except:
        pass
    return {"ok": True}


@router.get("/file/{file_id}")
def serve_file(file_id: int):
    ensure_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT stored_name, orig_name, mime FROM complaint_files WHERE id=?",
            (file_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "not found")
    fp = UPLOAD_DIR / row["stored_name"]
    if not fp.exists():
        raise HTTPException(404, "missing")
    return FileResponse(
        path=str(fp),
        media_type=row["mime"] or "application/octet-stream",
        filename=row["orig_name"] or fp.name,
    )


# ---- 取得明細（保留） ----
@router.get("/api/get/{complaint_id}")
def api_get(complaint_id: str):
    ensure_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM complaints WHERE ComplaintID=?", (complaint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        files = c.execute(
            "SELECT id, orig_name, mime, size FROM complaint_files WHERE complaint_id=? ORDER BY id",
            (row["id"],),
        ).fetchall()
    item = dict(row)
    item["Room"] = normalize_room(item.get("Room", ""))
    return {"item": item, "files": [dict(f) for f in files]}


# ---- Resolution Logs：查詢清單 ----
@router.get("/api/resolution/list/{complaint_id}")
def api_resolution_list(complaint_id: str):
    ensure_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT id FROM complaints WHERE ComplaintID=?", (complaint_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        logs = c.execute(
            """
            SELECT id, dept, author_name, content, created_by, created_at
            FROM complaint_resolution_logs
            WHERE complaint_id=?
            ORDER BY datetime(created_at) DESC, id DESC
            """,
            (row["id"],),
        ).fetchall()
    return {"ok": True, "items": [dict(x) for x in logs]}


# ---- Resolution Logs：新增一筆 ----
@router.post("/api/resolution/add")
async def api_resolution_add(
    payload: Dict,
    user: Optional[str] = Cookie(default=None),
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    # 安全起見：需具管理或編輯權才可新增版本記錄
    if not (_can_manage(role, permissions) or _can_edit(role, permissions)):
        raise HTTPException(403, "Forbidden")

    ensure_db()
    comp_id = _to_text(payload.get("ComplaintID") or payload.get("complaint_id"))
    dept = _to_text(payload.get("dept"))
    author = _to_text(payload.get("name") or payload.get("author_name"))
    content = _to_text(payload.get("content") or payload.get("resolution"))

    if not comp_id or not content:
        return JSONResponse(
            {"ok": False, "error": "complaint_id 與 content 為必填"}, 400
        )

    now = _now_str()
    with get_conn() as c:
        row = c.execute(
            "SELECT id FROM complaints WHERE ComplaintID=?",
            (comp_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "not found")

        c.execute(
            """
            INSERT INTO complaint_resolution_logs
            (complaint_id, dept, author_name, content, created_by, created_at)
            VALUES (?,?,?,?,?,?)
            """,
            (row["id"], dept, author, content, (user or ""), now),
        )

    return {"ok": True, "created_at": now}


# ---- 分析（保留原 keys，新增更多欄位） ----
@router.post("/api/analyze")
def api_analyze(
    role: Optional[str] = Cookie(None), permissions: Optional[str] = Cookie(None)
):
    if not (_is_admin(role, permissions) or _can_manage(role, permissions)):
        raise HTTPException(403, "Forbidden")

    ensure_db()
    with get_conn() as c:
        rows = c.execute(
            f"SELECT id,{','.join(HEADERS)} FROM complaints ORDER BY id DESC"
        ).fetchall()
    if not rows:
        return {
            "total_count": 0,
            "counts": [],
            "facilities": {"top": []},
            "auto_category": [],
            "engineering": [],
            "top_rooms": [],
            "by_floor": [],
            "hot_pairs_90d": [],
            "ai_note": "（沒有資料。）",
        }
    total = len(rows)
    ds = None
    de = None
    from collections import defaultdict

    cat_counter = defaultdict(int)
    eng_counter = defaultdict(int)
    room_counter = defaultdict(int)
    floor_counter = defaultdict(int)
    hot_counter = defaultdict(int)
    cutoff = datetime.now() - timedelta(days=90)
    missing_date = 0
    missing_room = 0
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        created_dt = parse_dt(
            d.get("Date", ""), d.get("Time", ""), d.get("CreatedAt", "")
        )
        if created_dt is None:
            missing_date += 1
        else:
            ds = created_dt if ds is None or created_dt < ds else ds
            de = created_dt if de is None or created_dt > de else de
        room = normalize_room(d.get("Room", ""))
        if not room:
            missing_room += 1
        else:
            room_counter[room] += 1
            fl = extract_floor(room)
            if fl is not None:
                floor_counter[fl] += 1
        cats = classify_text_by_db(d.get("Title", ""), d.get("Description", ""))
        primary = cats[0] if cats else "未分類"
        cat_counter[primary] += 1
        if primary in ENGINEERING_SET:
            eng_counter[primary] += 1
        if created_dt and created_dt >= cutoff and room:
            hot_counter[(room, primary)] += 1

    def to_sorted(dic, k1, k2, top=None):
        arr = [{k1: k, k2: v} for k, v in dic.items()]
        arr.sort(key=lambda x: x[k2], reverse=True)
        return arr if top is None else arr[:top]

    counts = to_sorted(cat_counter, "issue", "count", 10)
    engineering = to_sorted(eng_counter, "issue", "count")
    top_rooms = to_sorted(room_counter, "room", "count", 20)
    by_floor = to_sorted(floor_counter, "floor", "count")
    hot_pairs_90d = [
        {"room": k[0], "issue": k[1], "count": v}
        for (k, v) in hot_counter.items()
        if v >= 3
    ]
    hot_pairs_90d.sort(key=lambda x: x["count"], reverse=True)
    return {
        "total_count": total,
        "date_start": ds.strftime("%Y-%m-%d") if ds else None,
        "date_end": de.strftime("%Y-%m-%d") if de else None,
        "counts": counts,
        "facilities": {"top": engineering[:10]},
        "auto_category": counts,
        "engineering": engineering,
        "top_rooms": top_rooms,
        "by_floor": by_floor,
        "hot_pairs_90d": hot_pairs_90d,
        "data_quality": {
            "missing_date_pct": round(missing_date / total * 100, 2),
            "missing_room_pct": round(missing_room / total * 100, 2),
        },
        "ai_note": "（DB 規則＋統計產生；規則可於 complaint_category* 表維護。）",
    }


@router.get("/api/analyze")
def api_analyze(q: str = ""):
    """
    回傳 JSON：top_dept / top_room / daily_trend
    若權限/路由設定錯，至少也回 JSON 的錯誤格式，不會輸出 HTML。
    """
    ensure_db()
    like_params = []
    where_parts = []
    if (q or "").strip():
        like = f"%{q.strip()}%"
        # 依你前端搜尋習慣，挑常用欄位做 LIKE
        where_parts.append(
            "("
            + " OR ".join(
                [
                    "IFNULL(Title,'') LIKE ?",
                    "IFNULL(Description,'') LIKE ?",
                    "IFNULL(Dept,'') LIKE ?",
                    "IFNULL(Room,'') LIKE ?",
                    "IFNULL(Status,'') LIKE ?",
                    "IFNULL(Resolution,'') LIKE ?",
                ]
            )
            + ")"
        )
        like_params = [like] * 6

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    where_room_sql = (
        where_sql + (" AND " if where_sql else " WHERE ") + "IFNULL(Room,'') <> ''"
    )

    try:
        with get_conn() as c:
            top_dept = c.execute(
                f"""
                SELECT IFNULL(Dept,'') AS dept, COUNT(*) AS cnt
                FROM complaints
                {where_sql}
                GROUP BY Dept
                ORDER BY cnt DESC
                LIMIT 5
            """,
                like_params,
            ).fetchall()

            top_room = c.execute(
                f"""
                SELECT Room AS room, COUNT(*) AS cnt
                FROM complaints
                {where_room_sql}
                GROUP BY Room
                ORDER BY cnt DESC
                LIMIT 10
            """,
                like_params,
            ).fetchall()

            daily = c.execute(
                f"""
                SELECT Date AS date, COUNT(*) AS cnt
                FROM complaints
                {where_sql}
                GROUP BY Date
                ORDER BY Date ASC
            """,
                like_params,
            ).fetchall()

        return {
            "ok": True,
            "top_dept": {"items": [{"dept": r[0], "count": r[1]} for r in top_dept]},
            "top_room": {"items": [{"room": r[0], "count": r[1]} for r in top_room]},
            "daily_trend": {"items": [{"date": r[0], "count": r[1]} for r in daily]},
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# === Sankey / Network 圖譜 API（Category 更完整時優先採用，否則以 NLP 規則推論） ===
@router.get("/api/sankey")
def api_sankey(start: str = "", end: str = "", min_value: int = 1):
    """
    回傳 ECharts Sankey 所需資料：
    {
      "nodes": [{"name": "冷氣/空調"}, {"name": "工程"}, ...],
      "links": [{"source": "冷氣/空調", "target": "工程", "value": 12}, ...]
    }
    可選參數：
      - start, end：日期區間（含端點）
      - min_value：過濾小流量（預設 1）
    """
    ensure_db()

    def _primary_category(title: str, desc: str, cat_field: str) -> str:
        # 若 DB 的 Category 欄位有值，以它為主；否則用 NLP 規則。
        cat_field = _to_text(cat_field)
        if cat_field:
            return cat_field
        cats = classify_text_by_db(title, desc)
        return cats[0] if cats else "未分類"

    where, params = [], []
    if _to_text(start) and _to_text(end):
        where.append("Date BETWEEN ? AND ?")
        params += [start.strip(), end.strip()]
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as c:
        rows = c.execute(
            f"SELECT Category, Dept, Title, Description FROM complaints{where_sql}",
            params,
        ).fetchall()

    from collections import defaultdict

    flow = defaultdict(int)  # (cat -> dept) => count
    cats, depts = set(), set()

    for r in rows:
        cat = _primary_category(r["Title"], r["Description"], r["Category"])
        dept = _to_text(r["Dept"]) or "（未填部門）"
        cats.add(cat)
        depts.add(dept)
        flow[(cat, dept)] += 1

    # 構成 ECharts 節點/連結
    nodes = [{"name": n} for n in sorted(cats)] + [{"name": n} for n in sorted(depts)]
    links = [
        {"source": k[0], "target": k[1], "value": v}
        for k, v in flow.items()
        if v >= int(min_value or 1)
    ]
    return {"ok": True, "nodes": nodes, "links": links}


@router.get("/api/network")
def api_network(start: str = "", end: str = "", min_value: int = 1):
    """
    回傳 ECharts Graph（力導或關係圖）所需資料：
    {
      "nodes": [{"id":"冷氣/空調","name":"冷氣/空調","group":"Category","value":32}, ...],
      "links": [{"source":"冷氣/空調","target":"工程","value":12}, {"source":"冷氣/空調","target":"高","value":5}, {"source":"工程","target":"高","value":3}]
    }
    Edge 組成：
      1) Category → Dept   （該類別常由哪些部門處理）
      2) Category → Severity（該類別常見的嚴重度）
      3) Dept → Severity    （該部門經手案件的常見嚴重度）
    """
    ensure_db()

    def _primary_category(title: str, desc: str, cat_field: str) -> str:
        cat_field = _to_text(cat_field)
        if cat_field:
            return cat_field
        cats = classify_text_by_db(title, desc)
        return cats[0] if cats else "未分類"

    where, params = [], []
    if _to_text(start) and _to_text(end):
        where.append("Date BETWEEN ? AND ?")
        params += [start.strip(), end.strip()]
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with get_conn() as c:
        rows = c.execute(
            f"SELECT Category, Dept, Severity, Title, Description FROM complaints{where_sql}",
            params,
        ).fetchall()

    from collections import defaultdict

    # 節點計數，用來給節點 size（value）
    node_count = defaultdict(int)
    # 三種關聯的統計
    cat_dept = defaultdict(int)
    cat_sev = defaultdict(int)
    dept_sev = defaultdict(int)

    for r in rows:
        cat = _primary_category(r["Title"], r["Description"], r["Category"])
        dept = _to_text(r["Dept"]) or "（未填部門）"
        sev = _to_text(r["Severity"]) or "中"

        node_count[("Category", cat)] += 1
        node_count[("Dept", dept)] += 1
        node_count[("Severity", sev)] += 1

        cat_dept[(cat, dept)] += 1
        cat_sev[(cat, sev)] += 1
        dept_sev[(dept, sev)] += 1

    minv = int(min_value or 1)

    def _mk_nodes():
        nodes = []
        for (grp, name), val in node_count.items():
            nodes.append(
                {
                    "id": name,
                    "name": name,
                    "group": grp,  # Category / Dept / Severity
                    "value": val,  # 節點權重
                    "symbolSize": 10 + min(30, val),  # 適度放大
                }
            )
        return nodes

    def _mk_links(dic):
        return [
            {"source": s, "target": t, "value": v}
            for (s, t), v in dic.items()
            if v >= minv
        ]

    nodes = _mk_nodes()
    links = _mk_links(cat_dept) + _mk_links(cat_sev) + _mk_links(dept_sev)

    return {"ok": True, "nodes": nodes, "links": links}


# =========================
#   類別 / 規則維護 API（admin）
#   （不改版面；供後台或 Postman 操作）
# =========================
@router.get("/api/categories")
def api_categories(
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _is_admin(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    with get_conn() as c:
        cats = c.execute(
            "SELECT * FROM complaint_category ORDER BY sort_order,name"
        ).fetchall()
        pats = c.execute(
            """
            SELECT c.id AS category_id, c.name AS category, p.id AS pattern_id, p.pattern
            FROM complaint_category c LEFT JOIN complaint_category_pattern p ON p.category_id=c.id
            ORDER BY c.sort_order, c.name, p.id
        """
        ).fetchall()
    out = []
    by_id = {}
    for r in cats:
        item = dict(r)
        item["patterns"] = []
        out.append(item)
        by_id[item["id"]] = item
    for p in pats:
        cid = p["category_id"]
        if cid in by_id and p["pattern"] is not None:
            by_id[cid]["patterns"].append(
                {"id": p["pattern_id"], "pattern": p["pattern"]}
            )
    return out


@router.post("/api/categories")
async def api_categories_create(
    payload: Dict,
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _is_admin(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    name = _to_text(payload.get("name"))
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, 400)
    is_eng = 1 if payload.get("is_engineering") else 0
    active = 1 if payload.get("active", True) else 0
    sort_order = int(payload.get("sort_order", 100))
    now = _now_str()
    with get_conn() as c:
        c.execute(
            "INSERT INTO complaint_category (name,is_engineering,active,sort_order,updated_at) VALUES (?,?,?,?,?)",
            (name, is_eng, active, sort_order, now),
        )
        cid = c.execute(
            "SELECT id FROM complaint_category WHERE name=?", (name,)
        ).fetchone()["id"]
        for pat in payload.get("patterns", []) or []:
            if not _to_text(pat):
                continue
            c.execute(
                "INSERT INTO complaint_category_pattern (category_id,pattern,updated_at) VALUES (?,?,?)",
                (cid, _to_text(pat), now),
            )
    return {"ok": True}


@router.put("/api/categories/{cid}")
async def api_categories_update(
    cid: int,
    payload: Dict,
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _is_admin(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    now = _now_str()
    fields = []
    args = []
    for k in ["name", "is_engineering", "active", "sort_order"]:
        if k in payload:
            v = payload[k]
            if k in ["is_engineering", "active"]:
                v = 1 if v else 0
            fields.append(f"{k}=?")
            args.append(v)
    if fields:
        with get_conn() as c:
            c.execute(
                f"UPDATE complaint_category SET {', '.join(fields)}, updated_at=? WHERE id=?",
                args + [now, cid],
            )
    # patterns 可選：全替換
    if "patterns" in payload:
        pats = payload.get("patterns") or []
        with get_conn() as c:
            c.execute(
                "DELETE FROM complaint_category_pattern WHERE category_id=?", (cid,)
            )
            for pat in pats:
                if not _to_text(pat):
                    continue
                c.execute(
                    "INSERT INTO complaint_category_pattern (category_id,pattern,updated_at) VALUES (?,?,?)",
                    (cid, _to_text(pat), now),
                )
    return {"ok": True}


@router.delete("/api/categories/{cid}")
def api_categories_delete(
    cid: int,
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    if not _is_admin(role, permissions):
        raise HTTPException(403, "Forbidden")
    ensure_db()
    with get_conn() as c:
        c.execute("DELETE FROM complaint_category WHERE id=?", (cid,))
    return {"ok": True}


# =========================
#   每週熱點告警  ＋  SSE
# =========================
def week_start(dt: datetime) -> date:
    # 以週一為週起
    wd = dt.weekday()  # Mon=0
    return (dt - timedelta(days=wd)).date()


def compute_weekly_hot_alerts(
    base_dt: Optional[datetime] = None,
) -> Tuple[date, List[Dict]]:
    """回傳 (週起日, 熱點清單)；規則：近 90 天、房號×類別 ≥ 3。"""
    ensure_db()
    now = base_dt or datetime.now()
    ws = week_start(now)  # 本週一
    cutoff = now - timedelta(days=90)
    with get_conn() as c:
        rows = c.execute(f"SELECT {','.join(HEADERS)} FROM complaints").fetchall()
    from collections import defaultdict

    counter = defaultdict(int)
    for r in rows:
        created = parse_dt(r["Date"], r["Time"], r["CreatedAt"])
        if not created or created < cutoff:
            continue
        room = normalize_room(r["Room"])
        if not room:
            continue
        cats = classify_text_by_db(r["Title"], r["Description"])
        primary = cats[0] if cats else "未分類"
        counter[(room, primary)] += 1
    alerts = [
        {"week_start": ws.isoformat(), "room": k[0], "category": k[1], "count": v}
        for (k, v) in counter.items()
        if v >= 3
    ]
    alerts.sort(key=lambda x: x["count"], reverse=True)
    return ws, alerts


@router.get("/cron/weekly_hot")
def cron_weekly_hot(
    role: Optional[str] = Cookie(default=None),
    permissions: Optional[str] = Cookie(default=None),
):
    """排程用端點（交給 Windows 排程器/cron 每週一 09:00 打一次）。"""
    # 不限制權限，方便外部 scheduler 呼叫；若需限制可改 token 驗證
    ensure_db()
    ws, alerts = compute_weekly_hot_alerts()
    now = _now_str()
    with get_conn() as c:
        for a in alerts:
            # UPSERT 避免重複
            c.execute(
                """
                INSERT INTO complaint_hot_alert (week_start,room,category,count,created_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(week_start,room,category) DO UPDATE SET count=excluded.count, created_at=excluded.created_at
            """,
                (a["week_start"], a["room"], a["category"], a["count"], now),
            )
    return {"ok": True, "week_start": ws.isoformat(), "alerts": alerts}


@router.get("/sse/hot")
async def sse_weekly_hot():
    """SSE：連線即送出「本週」的告警清單，之後每 30 秒 keepalive。"""
    ensure_db()

    async def event_gen() -> Generator[bytes, None, None]:
        # 首包：本週資料
        ws = week_start(datetime.now()).isoformat()
        with get_conn() as c:
            rows = c.execute(
                "SELECT room, category, count FROM complaint_hot_alert WHERE week_start=? ORDER BY count DESC",
                (ws,),
            ).fetchall()
        data = [
            {"room": r["room"], "category": r["category"], "count": r["count"]}
            for r in rows
        ]
        first = {"week_start": ws, "alerts": data}
        yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n".encode("utf-8")
        # keepalive
        while True:
            await asyncio.sleep(30)
            yield b": keepalive\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")
