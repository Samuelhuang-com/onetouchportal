from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from app_utils import render_with_user

router = APIRouter()

@router.get("/games/ox", response_class=HTMLResponse)
async def ox_game(request: Request):
    return render_with_user(request, "games/ox.html")

@router.get("/games/snake", response_class=HTMLResponse)
async def snake_game(request: Request):
    return render_with_user(request, "games/Snake.html")

@router.get("/games/2048", response_class=HTMLResponse)
async def game_2048(request: Request):
    return render_with_user(request, "games/2048.html")

@router.get("/games/flappy", response_class=HTMLResponse)
async def flappy_game(request: Request):
    return render_with_user(request, "games/flappy.html")
