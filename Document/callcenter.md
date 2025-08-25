# OneTouch Callcenter (FastAPI + Excel)

> 放到你的專案結構：
>
> ```
> ├─ main.py
> ├─ routers/
> │  └─ callcenter_router.py
> ├─ templates/
> │  └─ callcenter/
> │     └─ callcenter.html
> └─ static/
>    ├─ css/
>    └─ js/
> ```
>
> 需求：使用 **Excel** 作為資料存放。程式會自動建立 `callcenter_data.xlsx`（或使用環境變數 `CALLCENTER_XLSX` 指定路徑）。
>
> 依賴：`openpyxl`（若沒有請安裝：`pip install openpyxl`）。

---

## 1) `main.py`

```python
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from routers.callcenter_router import router as callcenter_router

app = FastAPI(title="OneTouch Portal")

# 掛載靜態檔案（若不需要可移除）
app.mount("/static", StaticFiles(directory="static"), name="static")

# 掛載總機客服路由
app.include_router(callcenter_router)

# （可選）簡單首頁，導向總機客服頁
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("callcenter/callcenter.html", {"request": request})

# 若用 `python main.py` 執行可加這段
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
```

---

## 2) `routers/callcenter_router.py`

```python
import os
from pathlib import Path
from threading import Lock
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
    TAIPEI = ZoneInfo("Asia/Taipei")
except Exception:
    # 後備方案：固定 +08:00
    from datetime import timezone, timedelta
    TAIPEI = timezone(timedelta(hours=8))

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import status as http_status
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

router = APIRouter(prefix="/callcenter", tags=["callcenter"])
templates = Jinja2Templates(directory="templates")

# === 設定：Excel 路徑（可用環境變數覆寫） ===
EXCEL_PATH = Path(os.getenv("CALLCENTER_XLSX", "callcenter_data.xlsx")).resolve()
_excel_lock = Lock()

# === 工作表名稱與欄位 ===
SHEET_TICKETS = "Tickets"
SHEET_DEPTS = "Departments"
SHEET_AGENTS = "Agents"
SHEET_CATS = "Categories"

TICKET_HEADERS = [
    "TicketID","Date","AnswerTime","CallerName","Phone","Channel",
    "Category","Subcategory","Priority","NeedCallback","CallbackAt",
    "Issue","Dept","Assignee","RoomOrOrder","Owner",
    "DueDate","Status","Resolution","Note",
    "CreatedAt","CreatedBy","UpdatedAt"
]

# --- Excel 初始化與工具 ---

def _ensure_workbook():
    """確保工作簿與必要工作表/表頭存在。"""
    if not EXCEL_PATH.exists():
        EXCEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        # 預設會有一個叫 "Sheet" 的工作表，改名成 Tickets
        ws = wb.active
        ws.title = SHEET_TICKETS
        ws.append(TICKET_HEADERS)
        # 其他參照表
        wb.create_sheet(SHEET_DEPTS)
        wb.create_sheet(SHEET_AGENTS)
        wb.create_sheet(SHEET_CATS)
        # 放上表頭
        wb[SHEET_DEPTS].append(["DeptName"])  # A
        wb[SHEET_AGENTS].append(["DeptName","AgentName","Ext"])  # A,B,C
        wb[SHEET_CATS].append(["Category","Subcategory"])  # A,B
        # 一些示範資料（可自行刪除）
        wb[SHEET_DEPTS].append(["前台"])
        wb[SHEET_DEPTS].append(["餐飲"])
        wb[SHEET_DEPTS].append(["客務"])
        wb[SHEET_AGENTS].append(["前台","小張","101"])
        wb[SHEET_AGENTS].append(["餐飲","小李","201"])
        wb[SHEET_AGENTS].append(["客務","小王","301"])
        wb[SHEET_CATS].append(["訂房","訂金/退款"])
        wb[SHEET_CATS].ap
```
