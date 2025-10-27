# routers/execask_router.py
from __future__ import annotations

import datetime as dt
import json, re, sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

# ---- 路徑與資源 ----
BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR, SQL_DIR, DATA_DIR, TEMPLATES_DIR = (
    BASE_DIR / "config",
    BASE_DIR / "sql",
    BASE_DIR / "data",
    BASE_DIR / "templates",
)

# ---- OneTouch helpers（帶後備）----
templates: Optional[Jinja2Templates] = None
get_base_context = None
ensure_permission_pack = None
user_has_permission = None
get_conn = None

try:
    from app_utils import (  # type: ignore
        templates as _otp_templates,
        get_base_context as _otp_get_base_context,
        ensure_permission_pack as _otp_ensure_permission_pack,
        user_has_permission as _otp_user_has_permission,
        get_conn as _otp_get_conn,
    )

    templates = _otp_templates
    get_base_context = _otp_get_base_context
    ensure_permission_pack = _otp_ensure_permission_pack
    user_has_permission = _otp_user_has_permission
    get_conn = _otp_get_conn
except Exception:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def get_base_context(request: Request, active_menu: str = "") -> dict:
        return {"request": request, "active_menu": active_menu}

    def ensure_permission_pack(prefix: str):
        return None

    def user_has_permission(request: Request, perm_key: str) -> bool:
        perms = set(getattr(request, "session", {}).get("permissions", []))
        return perm_key in perms

    def get_conn(db_path: str):
        # 支援絕對/相對路徑；預設連 data/ 下檔案
        p = Path(db_path)
        return sqlite3.connect(str(p if p.is_absolute() else (DATA_DIR / db_path)))


# ---- YAML 後備 ----
try:
    import yaml  # type: ignore
except Exception:

    class _FakeYAML:
        @staticmethod
        def safe_load(s: str):
            try:
                return json.loads(s)
            except Exception:
                return None

    yaml = _FakeYAML()  # type: ignore

router = APIRouter()

# ---- 預設值（深度合併用）----
DEFAULT_ALIASES = {
    "outlets": {
        "北馥樓": ["北馥樓", "北馥", "Beifu"],
        "板石": ["板石", "BoardStone"],
    },
    "depts": {
        "客務部": ["客務部", "客務", "前台", "Front Desk"],
    },
}
DEFAULT_METRICS = {
    "revenue": ["營收", "營業額", "收入", "營業收入"],
    "occ": ["住房率", "OCC", "住房佔有率"],
    "complaints_count": ["客訴", "投訴", "抱怨", "客訴數", "客訴件數"],
    "approvals_pending": ["待簽", "待審", "簽核", "未完成簽核"],
}
DEFAULT_TABLES = {
    "eis": {
        "db": "eis.db",
        "revenue_table": "eis_revenue",
        "revenue_fields": {
            "date": "business_date",
            "outlet_key": "outlet_key",
            "outlet_name": "outlet_name",
            "amount": "net_amount",
        },
        "occ_table": "eis_occ_daily",
        "occ_fields": {"date": "business_date", "ratio": "occ_ratio"},
    },
    "complaints": {
        "db": "complaints.db",
        "table": "complaints",
        "fields": {"created_at": "created_at", "status": "status"},
    },
    "approvals": {
        "db": "approvals.db",
        "table": "approvals",
        "fields": {
            "status": "status",
            "assignee": "assignee",
            "created_at": "created_at",
        },
    },
}
DEFAULT_SETTINGS = {
    "timezone": "Asia/Taipei",
    "default_date": "yesterday",
    "openai": {"enabled": False, "model": "gpt-4o-mini", "max_tokens": 1024},
    "cache": {"alias_ttl_sec": 3600, "query_ttl_sec": 30},
    "logs": {"level": "INFO", "save_sql_for_admin": True},
    # 開發模式直通（僅本機驗證用；上線請改 False）
    "dev_mode_allow_all": True,
}


# ---- 讀檔 + 深度合併（忽略 None，不覆蓋預設結構）----
def _load_yaml(p: Path) -> dict:
    try:
        if p.exists():
            obj = yaml.safe_load(p.read_text(encoding="utf-8"))  # type: ignore
            return obj or {}
    except Exception:
        pass
    return {}


def _deep_merge(base: dict, override: dict) -> dict:
    res = deepcopy(base)
    if not isinstance(override, dict):
        return res
    for k, v in override.items():
        if v is None:
            # 忽略 None，保留預設
            continue
        if isinstance(v, dict) and isinstance(res.get(k), dict):
            res[k] = _deep_merge(res.get(k, {}), v)
        else:
            res[k] = v
    return res


ALIASES = _deep_merge(DEFAULT_ALIASES, _load_yaml(CONFIG_DIR / "execask_aliases.yml"))
METRICS = _deep_merge(DEFAULT_METRICS, _load_yaml(CONFIG_DIR / "execask_metrics.yml"))
TABLES = _deep_merge(DEFAULT_TABLES, _load_yaml(CONFIG_DIR / "execask_tables.yml"))
SETTINGS = _deep_merge(
    DEFAULT_SETTINGS, _load_yaml(CONFIG_DIR / "execask_settings.yml")
)

# ---- 確保權限鍵存在 ----
try:
    ensure_permission_pack("execask")  # type: ignore
except Exception:
    pass


# ---- 安全工具（容忍 None/型別錯誤）----
def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        return [v]
    return []


def _has_perm(request: Request, perm_key: str) -> bool:
    if SETTINGS.get("dev_mode_allow_all"):
        return True
    return bool(user_has_permission(request, perm_key))  # type: ignore


# ---- 規則式 NLU ----
def _normalize_text(s: str) -> str:
    return re.sub(
        r"\s+", " ", s.replace("／", "/").replace("～", "~").replace("－", "-")
    ).strip()


def _parse_date_range(q: str) -> Tuple[str, str]:
    today = dt.date.today()
    if "昨天" in q:
        d = today - dt.timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if "今天" in q or "本日" in q:
        return today.isoformat(), today.isoformat()
    if "上週" in q:
        wd = today.weekday()
        end = today - dt.timedelta(days=wd + 1)
        start = end - dt.timedelta(days=6)
        return start.isoformat(), end.isoformat()
    if "上月" in q or "上個月" in q:
        first = today.replace(day=1)
        end = first - dt.timedelta(days=1)
        start = end.replace(day=1)
        return start.isoformat(), end.isoformat()
    m = re.search(r"(\d{1,2})/(\d{1,2})\s*[-~]\s*(\d{1,2})/(\d{1,2})", q)
    if m:
        y = today.year
        m1, d1, m2, d2 = map(int, m.groups())
        a = dt.date(y, m1, d1)
        b = dt.date(y, m2, d2)
        if a > b:
            a, b = b, a
        return a.isoformat(), b.isoformat()
    m = re.search(r"(?:(\d{4})/)?(\d{1,2})/(\d{1,2})", q)
    if m:
        y = int(m.group(1)) if m.group(1) else today.year
        mm = int(m.group(2))
        dd = int(m.group(3))
        d = dt.date(y, mm, dd)
        return d.isoformat(), d.isoformat()
    d = today - dt.timedelta(days=1)
    return d.isoformat(), d.isoformat()


def _resolve_entity(q: str) -> Dict[str, str]:
    aliases = _as_dict(ALIASES)
    # outlets
    for canon, alist in _as_dict(aliases.get("outlets")).items():
        for a in _as_list(alist):
            if a and a in q:
                return {"type": "outlet", "key": canon}
    # depts
    for canon, alist in _as_dict(aliases.get("depts")).items():
        for a in _as_list(alist):
            if a and a in q:
                return {"type": "dept", "key": canon}
    return {}


def _resolve_metric(q: str) -> Optional[str]:
    metrics = _as_dict(METRICS)
    for mcode, kws in metrics.items():
        for k in _as_list(kws):
            if k and k in q:
                return mcode
    if any(x in q for x in ["營收", "營業額", "收入"]):
        return "revenue"
    return None


# ---- SQL helpers ----
def _render_sql_template(name: str, mapping: dict) -> str:
    text = (SQL_DIR / name).read_text(encoding="utf-8")

    def repl(m):
        cur: Any = mapping
        for key in m.group(1).strip().split("."):
            cur = cur.get(key, "") if isinstance(cur, dict) else ""
        return str(cur) if isinstance(cur, (str, int, float)) else ""

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, text)


def _db_conn(db_file: str):
    try:
        return get_conn(db_file)  # type: ignore
    except Exception:
        p = Path(db_file)
        return sqlite3.connect(str(p if p.is_absolute() else (DATA_DIR / db_file)))


# ---- executors（含設定檢查）----
def _exec_revenue(entity: Dict[str, str], dfrom: str, dto: str) -> Dict[str, Any]:
    cfg_all = _as_dict(TABLES)
    cfg = _as_dict(cfg_all.get("eis"))
    rev_table = cfg.get("revenue_table")
    rev_fields = _as_dict(cfg.get("revenue_fields"))
    if (
        not rev_table
        or not rev_fields
        or not rev_fields.get("date")
        or not rev_fields.get("amount")
    ):
        raise HTTPException(status_code=500, detail="tables_config_invalid:eis.revenue")

    sql_name = "eis_revenue_day.sql" if dfrom == dto else "eis_revenue_range.sql"
    sql = _render_sql_template(
        sql_name,
        {
            "eis": {
                "revenue_table": rev_table,
                "date_field": rev_fields["date"],
                "outlet_key_field": rev_fields.get("outlet_key", "outlet_key"),
                "outlet_name_field": rev_fields.get("outlet_name", "outlet_name"),
                "amount_field": rev_fields["amount"],
            }
        },
    )
    params: Dict[str, Any] = {"dfrom": dfrom, "dto": dto, "outlet_key": None}
    if entity.get("type") == "outlet":
        params["outlet_key"] = entity["key"]

    with _db_conn(cfg.get("db", "eis.db")) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    total = 0.0
    if rows:
        if "revenue" in rows[0]:
            total = sum(float(r.get("revenue", 0) or 0) for r in rows)
        elif "sum_amount" in rows[0]:
            total = float(rows[0]["sum_amount"] or 0)
    return {
        "answer": {
            "value": total,
            "unit": "NT$",
            "granularity": "day" if dfrom == dto else "range",
        },
        "sources": [
            {"db": cfg.get("db", "eis.db"), "table": rev_table, "row_count": len(rows)}
        ],
        "raw_rows": rows,
        "sql_name": sql_name,
        "sql_params": params,
    }


def _exec_occ(entity: Dict[str, str], dfrom: str, dto: str) -> Dict[str, Any]:
    cfg_all = _as_dict(TABLES)
    cfg = _as_dict(cfg_all.get("eis"))
    occ_table = cfg.get("occ_table")
    occ_fields = _as_dict(cfg.get("occ_fields"))
    if (
        not occ_table
        or not occ_fields
        or not occ_fields.get("date")
        or not occ_fields.get("ratio")
    ):
        raise HTTPException(status_code=500, detail="tables_config_invalid:eis.occ")

    sql = (
        (SQL_DIR / "eis_occ_day.sql")
        .read_text(encoding="utf-8")
        .replace("{{eis.occ_table}}", occ_table)
        .replace("{{eis.occ_date_field}}", occ_fields["date"])
        .replace("{{eis.occ_ratio_field}}", occ_fields["ratio"])
    )

    with _db_conn(cfg.get("db", "eis.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, {"d": dfrom}).fetchone()
    ratio = float(row["occ_ratio"]) if row and ("occ_ratio" in row.keys()) else 0.0
    return {
        "answer": {"value": ratio, "unit": "%", "granularity": "day"},
        "sources": [
            {
                "db": cfg.get("db", "eis.db"),
                "table": occ_table,
                "row_count": 1 if row else 0,
            }
        ],
        "raw_rows": [dict(row)] if row else [],
        "sql_name": "eis_occ_day.sql",
        "sql_params": {"d": dfrom},
    }


def _exec_complaints_count(dfrom: str, dto: str) -> Dict[str, Any]:
    cfg = _as_dict(_as_dict(TABLES).get("complaints"))
    table = cfg.get("table")
    fields = _as_dict(cfg.get("fields"))
    if not table or not fields.get("created_at"):
        raise HTTPException(status_code=500, detail="tables_config_invalid:complaints")

    sql = (
        (SQL_DIR / "complaints_count.sql")
        .read_text(encoding="utf-8")
        .replace("{{complaints.table}}", table)
        .replace("{{complaints.created_at}}", fields["created_at"])
        .replace("{{complaints.status}}", fields.get("status", "status"))
    )

    with _db_conn(cfg.get("db", "complaints.db")) as conn:
        row = conn.execute(sql, {"dfrom": dfrom, "dto": dto}).fetchone()
    cnt = int(row[0]) if row else 0
    return {
        "answer": {
            "value": cnt,
            "unit": "件",
            "granularity": "range" if dfrom != dto else "day",
        },
        "sources": [
            {"db": cfg.get("db", "complaints.db"), "table": table, "row_count": cnt}
        ],
        "raw_rows": [],
        "sql_name": "complaints_count.sql",
        "sql_params": {"dfrom": dfrom, "dto": dto},
    }


def _exec_approvals_pending(dfrom: str, dto: str) -> Dict[str, Any]:
    cfg = _as_dict(_as_dict(TABLES).get("approvals"))
    table = cfg.get("table")
    fields = _as_dict(cfg.get("fields"))
    if not table or not fields.get("status") or not fields.get("created_at"):
        raise HTTPException(status_code=500, detail="tables_config_invalid:approvals")

    sql = (
        (SQL_DIR / "approvals_pending.sql")
        .read_text(encoding="utf-8")
        .replace("{{approvals.table}}", table)
        .replace("{{approvals.status}}", fields["status"])
        .replace("{{approvals.created_at}}", fields["created_at"])
    )

    with _db_conn(cfg.get("db", "approvals.db")) as conn:
        row = conn.execute(sql, {"dfrom": dfrom, "dto": dto}).fetchone()
    cnt = int(row[0]) if row else 0
    return {
        "answer": {
            "value": cnt,
            "unit": "筆",
            "granularity": "range" if dfrom != dto else "day",
        },
        "sources": [
            {"db": cfg.get("db", "approvals.db"), "table": table, "row_count": cnt}
        ],
        "raw_rows": [],
        "sql_name": "approvals_pending.sql",
        "sql_params": {"dfrom": dfrom, "dto": dto},
    }


# ---- 導覽建議 ----
def _navigation(entity: Dict[str, str], dfrom: str, dto: str) -> list:
    nav = [{"label": "在行事曆顯示", "href": f"/calendar?{urlencode({'date': dfrom})}"}]
    if entity and entity.get("type") == "outlet":
        nav.append(
            {
                "label": "EIS 營收報表",
                "href": f"/eis?date={dfrom}&outlet={quote(entity['key'])}",
            }
        )
    return nav


# ---- Routes & Health ----
@router.get("/execask")
async def page_execask(request: Request):
    ctx = get_base_context(request, active_menu="execask")  # type: ignore
    ctx.update({"title": "Executive Ask"})
    return templates.TemplateResponse("execask/execask.html", ctx)  # type: ignore


@router.get("/api/execask/health")
@router.get("/execask/health")
async def execask_health():
    return {"ok": True, "ts": dt.datetime.now().isoformat(timespec="seconds")}


@router.get("/api/execask/debug-perms")
@router.get("/execask/debug-perms")
async def execask_debug_perms(request: Request):
    return {
        "execask_view": _has_perm(request, "execask_view"),
        "execask_admin": _has_perm(request, "execask_admin"),
    }


# ---- Query API（支援 GET/POST；避免重複 /api）----
@router.api_route("/api/execask/query", methods=["POST", "GET"])
@router.api_route("/execask/query", methods=["POST", "GET"])
async def api_execask_query(request: Request):
    if not _has_perm(request, "execask_view"):
        raise HTTPException(
            status_code=403, detail="Permission denied: execask_view required"
        )

    if request.method == "GET":
        q = _normalize_text(str(request.query_params.get("q", "")))
    else:
        body = await request.json()
        q = _normalize_text(str(body.get("q", "")))

    if not q:
        return JSONResponse({"error": "query_is_empty"}, status_code=400)

    dfrom, dto = _parse_date_range(q)
    entity, metric = _resolve_entity(q), _resolve_metric(q)
    if not metric:
        return JSONResponse({"error": "metric_not_recognized"}, status_code=400)

    if metric == "revenue":
        result = _exec_revenue(entity, dfrom, dto)
    elif metric == "occ":
        result = _exec_occ(entity, dfrom, dfrom)
    elif metric == "complaints_count":
        result = _exec_complaints_count(dfrom, dto)
    elif metric == "approvals_pending":
        result = _exec_approvals_pending(dfrom, dto)
    else:
        return JSONResponse(
            {"error": f"metric_not_supported: {metric}"}, status_code=400
        )

    resp: Dict[str, Any] = {
        "intent": metric,
        "filters": {"entity": entity, "date": {"from": dfrom, "to": dto}},
        "answer": result.get("answer"),
        "sources": result.get("sources", []),
        "navigation": _navigation(entity, dfrom, dto),
    }

    if _has_perm(request, "execask_admin") and SETTINGS.get("logs", {}).get(
        "save_sql_for_admin", True
    ):
        resp.update(
            {
                "sql": {
                    "name": result.get("sql_name"),
                    "params": result.get("sql_params"),
                }
            }
        )
        if "raw_rows" in result:
            resp["raw_rows"] = result["raw_rows"]

    return JSONResponse(resp)
