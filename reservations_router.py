from fastapi import APIRouter, Request, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse
from typing import Optional
import json

# 從 app_utils 匯入共用的函式和物件
from app_utils import get_base_context, templates

router = APIRouter()

@router.get("/reservations", response_class=HTMLResponse)
async def reservations_page(
    request: Request, 
    user: Optional[str] = Cookie(None), 
    role: Optional[str] = Cookie(None), 
    permissions: Optional[str] = Cookie(None)
):
    if not user:
        return RedirectResponse(url="/")

    ctx = get_base_context(request, user, role, permissions)
    # 權限檢查
    if role != 'admin' and not ctx['permissions'].get('reservations'):
        return RedirectResponse(url="/dashboard")

    try:
        with open("data/reservations.json", encoding="utf-8") as f:
            raw_data = json.load(f)
    except Exception as e:
        print(f"Error reading reservations.json: {e}")
        raw_data = {}
        
    reservation_list = []
    for location, records in raw_data.items():
        for r in records:
            reservation_list.append({"location": location, **r})
            
    ctx["reservations"] = reservation_list
    return templates.TemplateResponse("reservations.html", ctx)
