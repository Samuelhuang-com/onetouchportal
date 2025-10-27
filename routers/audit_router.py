# routers/audit_router.py
from fastapi import APIRouter, Request, Cookie
from pydantic import BaseModel
from typing import Optional, Dict, Any
from data.audit_service import log_action, get_conn

router = APIRouter(tags=["Audit"])

class FeActionIn(BaseModel):
    action_name: str
    meta: Optional[Dict[str, Any]] = None

@router.post("/audit/fe-action")
async def fe_action(inb: FeActionIn, request: Request, user: Optional[str] = Cookie(None)):
    if not user:
        return {"ok": False, "error": "unauthorized"}
    log_action(request, inb.action_name, meta=inb.meta)
    return {"ok": True}

@router.get("/audit/top-pages")
async def top_pages(days: int = 7):
    q = """
    SELECT path, COUNT(*) AS views
    FROM audit_events
    WHERE event_type='page_view' AND created_at >= datetime('now', ?)
    GROUP BY path ORDER BY views DESC LIMIT 50
    """
    with get_conn() as conn:
        rows = conn.execute(q, (f"-{int(days)} days",)).fetchall()
    return [{"path": r[0], "views": r[1]} for r in rows]

@router.get("/audit/top-actions")
async def top_actions(days: int = 30):
    q = """
    SELECT action_name, COUNT(*) AS times
    FROM audit_events
    WHERE event_type='action' AND created_at >= datetime('now', ?)
    GROUP BY action_name ORDER BY times DESC LIMIT 50
    """
    with get_conn() as conn:
        rows = conn.execute(q, (f"-{int(days)} days",)).fetchall()
    return [{"action_name": r[0], "times": r[1]} for r in rows]
