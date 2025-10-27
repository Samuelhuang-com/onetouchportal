# routers/rooms_router.py
from fastapi import APIRouter, Request, Depends, Form, Cookie  # 確保 Cookie 已被引入
from typing import Optional  # 確保 Optional 已被引入
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse
from app_utils import get_base_context, templates, check_permission  # 移除無用的 import
from data.db import get_conn_rooms
import json

router = APIRouter(tags=["Rooms"])


# --- 一般房間總覽頁 (已修正) ---
@router.get("/rooms", response_class=HTMLResponse)
async def rooms_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission_flag: bool = Depends(check_permission("rooms_view")),
):
    if not has_permission_flag:
        return RedirectResponse(url="/dashboard?error=permission_denied")
    ctx = get_base_context(request, user, role, permissions)
    return templates.TemplateResponse("rooms.html", ctx)


# --- 管理員維護頁 (請修改此處) ---
@router.get("/rooms/manage", response_class=HTMLResponse)
async def rooms_manage_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
    has_permission_flag: bool = Depends(check_permission("rooms_manage")),
):
    if not has_permission_flag:
        return RedirectResponse(url="/dashboard?error=permission_denied")

    # 和 rooms_page 一樣，手動建立 context
    ctx = get_base_context(request, user, role, permissions)

    # 使用 templates.TemplateResponse 渲染
    return templates.TemplateResponse("rooms_manage.html", ctx)


# --- 搜尋 ---
@router.get("/rooms/api/search")
async def search_rooms(q: str = ""):
    # 關鍵字前後加上 '%' 進行模糊比對
    q_like = f"%{q}%"
    with get_conn_rooms() as c:
        rows = c.execute(
            """SELECT room_no, floor, type, attributes, features, image
               FROM rooms
               WHERE room_no LIKE ? 
                  OR floor LIKE ?      -- ← 新增對 floor 欄位的搜尋
                  OR type LIKE ? 
                  OR attributes LIKE ? 
                  OR features LIKE ?""",
            (q_like, q_like, q_like, q_like, q_like),  # ← 對應新增的欄位，多一個 q_like
        ).fetchall()
    return [dict(r) for r in rows]


# --- 取得全部 (維護用) ---
@router.get("/rooms/api/list")
async def list_rooms():
    with get_conn_rooms() as c:
        rows = c.execute(
            "SELECT id, room_no, floor, type, attributes, features, image, created_at FROM rooms  ORDER BY  room_no"
        ).fetchall()
    return [dict(r) for r in rows]


# --- 取得單一 ---
@router.get("/rooms/api/room/{room_no}")
async def get_room(room_no: str):
    with get_conn_rooms() as c:
        r = c.execute("SELECT * FROM rooms WHERE room_no=?", (room_no,)).fetchone()
    if not r:
        return JSONResponse({"error": "房號不存在"}, status_code=404)
    return dict(r)


# --- 新增/更新 ---
@router.post("/rooms/api/manage")
async def manage_room(
    room_no: str = Form(...),
    floor: str = Form(...),
    type: str = Form(...),
    attributes: str = Form("[]"),
    features: str = Form("[]"),
    image: str = Form(""),
    has_permission_flag: bool = Depends(check_permission("rooms_manage")),
):
    if not has_permission_flag:
        return JSONResponse({"error": "沒有權限"}, status_code=403)

    with get_conn_rooms() as c:
        c.execute(
            """INSERT OR REPLACE INTO rooms(room_no, floor, type, attributes, features, image)
               VALUES (?,?,?,?,?,?)""",
            (room_no, floor, type, attributes, features, image),
        )
    return {"ok": True}


# --- 刪除 ---
@router.post("/rooms/api/delete")
async def delete_room(
    room_no: str = Form(...),
    has_permission_flag: bool = Depends(check_permission("rooms_manage")),
):
    if not has_permission_flag:
        return JSONResponse({"error": "沒有權限"}, status_code=403)
    with get_conn_rooms() as c:
        c.execute("DELETE FROM rooms WHERE room_no=?", (room_no,))
    return {"ok": True}
