# routers/handover_router.py
from fastapi import APIRouter, Request, Depends, HTTPException, Form
from typing import Optional
import sqlite3, json, datetime
from app_utils import render_with_user, get_db, require_perm, _has_perm

router = APIRouter(prefix="/handover", tags=["handover"])

CATEGORIES = {
    "arrivals",
    "in_house",
    "departure",
    "opening",
    "h_room",
    "future_group",
    "lnf",
    "memo",
    "company_task",
}


def _today():
    return datetime.date.today().isoformat()


def _fetchall(conn, sql, params=()):
    c = conn.execute(sql, params)
    cols = [d[0] for d in c.description]
    return [dict(zip(cols, r)) for r in c.fetchall()]


@router.get("/{category}")
def list_page(
    request: Request,
    category: str,
    date: Optional[str] = None,
    shift: Optional[str] = None,
    final: Optional[str] = None,
    q: Optional[str] = None,
):
    require_perm(request, "handover_view")
    if category not in CATEGORIES:
        raise HTTPException(404, "Unknown category")
    date = date or _today()
    conn = get_db("data/fddata.db")
    params = [category, date]
    where = ["category=?", "date=?"]
    if final in ("draft", "final"):
        where.append("status=?")
        params.append(final)
    if shift:
        where.append("shift=?")
        params.append(shift)
    if q:
        where.append("(notes LIKE ? OR source LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])

    rows = _fetchall(
        conn,
        f"""
      SELECT id, date, shift, notes, prepared_by, final_by, final_date, status,
             stay_id, party_id, block_id, room_id, source, extras_json
      FROM handover_items
      WHERE {" AND ".join(where)}
      ORDER BY id DESC
    """,
        params,
    )

    ctx = {
        "page_title": f"交班事項 · {category}",
        "category": category,
        "date": date,
        "shift": shift or "",
        "final": final or "",
        "rows": rows,
        "can_manage": _has_perm(request, "handover_manage"),
        "can_admin": _has_perm(request, "handover_admin"),
    }
    return render_with_user(request, "handover/list.html", extra=ctx)


@router.post("/{category}/create")
def create_item(
    request: Request,
    category: str,
    date: str = Form(...),
    shift: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    source: Optional[str] = Form(None),
    stay_id: Optional[int] = Form(None),
    party_id: Optional[int] = Form(None),
    block_id: Optional[int] = Form(None),
    room_id: Optional[int] = Form(None),
    extras_json: Optional[str] = Form("{}"),
):
    require_perm(request, "handover_manage")
    if category not in CATEGORIES:
        raise HTTPException(404, "Unknown category")
    user_id = request.state.user["id"]
    try:
        extras = json.loads(extras_json or "{}")
    except Exception:
        raise HTTPException(422, "extras_json must be valid JSON")
    conn = get_db("data/fddata.db")
    with conn:
        c = conn.execute(
            """
          INSERT INTO handover_items(category,date,shift,notes,prepared_by,status,
                                     stay_id,party_id,block_id,room_id,source,extras_json)
          VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?)
        """,
            (
                category,
                date,
                shift,
                notes,
                user_id,
                stay_id,
                party_id,
                block_id,
                room_id,
                source,
                json.dumps(extras),
            ),
        )
        item_id = c.lastrowid
        conn.execute(
            """
          INSERT INTO audit_log(table_name,row_id,action,after_json,acted_by)
          VALUES('handover_items',?,'INSERT',?,?)
        """,
            (item_id, json.dumps({"id": item_id, "category": category}), user_id),
        )
    return {"ok": True, "id": item_id}


@router.post("/{item_id}/finalize")
def finalize_item(request: Request, item_id: int):
    require_perm(request, "handover_manage")  # finalize 屬於 manage；reopen 需 admin
    user_id = request.state.user["id"]
    conn = get_db("data/fddata.db")
    with conn:
        row = _fetchall(conn, "SELECT * FROM handover_items WHERE id=?", (item_id,))
        if not row:
            raise HTTPException(404, "Not found")
        if row[0]["status"] == "final":
            return {"ok": True, "id": item_id, "status": "final"}
        conn.execute(
            """
          UPDATE handover_items
          SET status='final', final_by=?, final_date=strftime('%Y-%m-%dT%H:%M:%fZ','now')
          WHERE id=?
        """,
            (user_id, item_id),
        )
        conn.execute(
            """
          INSERT INTO audit_log(table_name,row_id,action,before_json,after_json,acted_by)
          VALUES('handover_items',?,'FINALIZE',?, ?,?)
        """,
            (item_id, json.dumps(row[0]), json.dumps({"status": "final"}), user_id),
        )
    return {"ok": True, "id": item_id, "status": "final"}


@router.post("/{item_id}/reopen")
def reopen_item(request: Request, item_id: int):
    require_perm(request, "handover_admin")
    user_id = request.state.user["id"]
    conn = get_db("data/fddata.db")
    with conn:
        row = _fetchall(conn, "SELECT * FROM handover_items WHERE id=?", (item_id,))
        if not row:
            raise HTTPException(404, "Not found")
        conn.execute("UPDATE handover_items SET status='draft' WHERE id=?", (item_id,))
        conn.execute(
            """
          INSERT INTO audit_log(table_name,row_id,action,before_json,after_json,acted_by)
          VALUES('handover_items',?,'REOPEN',?, ?,?)
        """,
            (item_id, json.dumps(row[0]), json.dumps({"status": "draft"}), user_id),
        )
    return {"ok": True, "id": item_id, "status": "draft"}
