from fastapi import (
    APIRouter,
    Request,
    Form,
    Cookie,
    UploadFile,
    File,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    FileResponse,
)
from typing import Optional, Dict, Any
import pandas as pd
from datetime import datetime
import os
import shutil
import uuid

# 從 app_utils 匯入共用的函式和物件
from app_utils import (
    get_base_context,
    templates,
    CONTRACTS_FILE,
    CONTRACT_ATTACHMENT_DIR,
)

router = APIRouter()

# --- 合約管理的輔助函式 ---


def find_attachment(contract_id: str) -> Optional[str]:
    """根據 ContractID 尋找對應的附件檔名"""
    os.makedirs(CONTRACT_ATTACHMENT_DIR, exist_ok=True)
    for filename in os.listdir(CONTRACT_ATTACHMENT_DIR):
        if filename.startswith(contract_id):
            return filename
    return None


def delete_attachment(contract_id: str):
    """刪除與 ContractID 相關的附件"""
    attachment = find_attachment(contract_id)
    if attachment:
        os.remove(os.path.join(CONTRACT_ATTACHMENT_DIR, attachment))


def read_contracts():
    expected_columns = [
        "ContractID",
        "VendorName",
        "ContractType",
        "Subject",
        "Content",
        "Amount",
        "StartDate",
        "EndDate",
        "ContactPerson",
        "ContactPhone",
        "Status",
        "Notes",
    ]
    if not os.path.exists(CONTRACTS_FILE) or os.path.getsize(CONTRACTS_FILE) == 0:
        return pd.DataFrame(columns=expected_columns)
    df = pd.read_excel(CONTRACTS_FILE, engine="openpyxl")
    if "StartDate" in df.columns:
        df["StartDate"] = pd.to_datetime(df["StartDate"])
    if "EndDate" in df.columns:
        df["EndDate"] = pd.to_datetime(df["EndDate"])
    return df


def save_contracts(df):
    os.makedirs("data", exist_ok=True)
    if "StartDate" in df.columns:
        df["StartDate"] = pd.to_datetime(df["StartDate"])
    if "EndDate" in df.columns:
        df["EndDate"] = pd.to_datetime(df["EndDate"])
    df = df.sort_values(by="EndDate", ascending=False).reset_index(drop=True)
    df.to_excel(CONTRACTS_FILE, index=False, engine="openpyxl")


async def get_contract_form_data(form: Any) -> Dict:
    return {
        "VendorName": form.get("VendorName"),
        "ContractType": form.get("ContractType"),
        "Subject": form.get("Subject"),
        "Content": form.get("Content"),
        "Amount": float(form.get("Amount", 0)),
        "Status": form.get("Status"),
        "StartDate": pd.to_datetime(form.get("StartDate")),
        "EndDate": pd.to_datetime(form.get("EndDate")),
        "ContactPerson": form.get("ContactPerson"),
        "ContactPhone": form.get("ContactPhone"),
        "Notes": form.get("Notes"),
    }


# --- 合約管理的路由 ---


@router.get("/contracts", response_class=HTMLResponse)
async def list_contracts(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")

    ctx = get_base_context(request, user, role, permissions)
    if role != "admin" and not ctx["permissions"].get("contracts"):
        return RedirectResponse(url="/dashboard")

    df = read_contracts()
    if df.empty or "EndDate" not in df.columns:
        ctx["contracts"] = []
        return templates.TemplateResponse("contracts_list.html", ctx)

    today = datetime.now()

    def get_expiry_info(end_date):
        if pd.isna(end_date):
            return "safe", "無效日期"
        delta = (end_date - today).days
        if delta < 0:
            return "expiry-level-expired", "已過期"
        elif delta <= 30:
            return "expiry-level-critical", f"{delta} 天後到期"
        elif delta <= 90:
            return "expiry-level-warning", f"{delta} 天後到期"
        else:
            return "expiry-level-safe", "90天以上"

    expiry_data = df["EndDate"].apply(get_expiry_info).apply(pd.Series)
    expiry_data.columns = ["expiry_level", "expiry_text"]
    df = pd.concat([df, expiry_data], axis=1)
    df["attachment_filename"] = df["ContractID"].apply(find_attachment)
    ctx["contracts"] = df.to_dict("records")
    return templates.TemplateResponse("contracts_list.html", ctx)


@router.get("/contracts/manage", response_class=HTMLResponse)
async def manage_contracts_page(
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")

    ctx = get_base_context(request, user, role, permissions)
    if role != "admin" and not ctx["permissions"].get("contracts"):
        return RedirectResponse(url="/dashboard")

    df = read_contracts()
    df["attachment_filename"] = df["ContractID"].apply(find_attachment)
    ctx.update(
        {
            "all_contracts": df.to_dict("records"),
            "action_url": router.url_path_for("add_contract"),
            "contract": None,
        }
    )
    return templates.TemplateResponse("manage_contract.html", ctx)


@router.get("/contracts/edit/{contract_id}", response_class=HTMLResponse)
async def edit_contract_page(
    contract_id: str,
    request: Request,
    user: Optional[str] = Cookie(None),
    role: Optional[str] = Cookie(None),
    permissions: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")

    ctx = get_base_context(request, user, role, permissions)
    if role != "admin" and not ctx["permissions"].get("contracts"):
        return RedirectResponse(url="/dashboard")

    df = read_contracts()
    df["attachment_filename"] = df["ContractID"].apply(find_attachment)
    contract_data = df[df["ContractID"] == contract_id].to_dict("records")
    if not contract_data:
        return RedirectResponse(url=router.url_path_for("manage_contracts_page"))

    ctx.update(
        {
            "all_contracts": df.to_dict("records"),
            "action_url": router.url_path_for("edit_contract", contract_id=contract_id),
            "contract": contract_data[0],
        }
    )
    return templates.TemplateResponse("manage_contract.html", ctx)


@router.post("/contracts/add")
async def add_contract(
    request: Request,
    attachment: UploadFile = File(None),
    user: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")

    form_data = await get_contract_form_data(await request.form())
    contract_id = str(uuid.uuid4())
    form_data["ContractID"] = contract_id
    if attachment and attachment.filename:
        delete_attachment(contract_id)
        file_extension = os.path.splitext(attachment.filename)[1]
        new_filename = f"{contract_id}{file_extension}"
        file_path = os.path.join(CONTRACT_ATTACHMENT_DIR, new_filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(attachment.file, buffer)
    df = read_contracts()
    new_record = pd.DataFrame([form_data])
    df = pd.concat([df, new_record], ignore_index=True)
    save_contracts(df)
    return RedirectResponse(
        url=router.url_path_for("manage_contracts_page"), status_code=303
    )


@router.post("/contracts/edit/{contract_id}")
async def edit_contract(
    contract_id: str,
    request: Request,
    attachment: UploadFile = File(None),
    user: Optional[str] = Cookie(None),
):
    if not user:
        return RedirectResponse(url="/")

    if attachment and attachment.filename:
        delete_attachment(contract_id)
        file_extension = os.path.splitext(attachment.filename)[1]
        new_filename = f"{contract_id}{file_extension}"
        file_path = os.path.join(CONTRACT_ATTACHMENT_DIR, new_filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(attachment.file, buffer)
    df = read_contracts()
    form_data = await get_contract_form_data(await request.form())
    idx = df.index[df["ContractID"] == contract_id].tolist()
    if idx:
        for key, value in form_data.items():
            df.loc[idx[0], key] = value
        save_contracts(df)
    return RedirectResponse(
        url=router.url_path_for("manage_contracts_page"), status_code=303
    )


@router.post("/contracts/delete")
async def delete_contract(
    request: Request, contract_id: str = Form(...), user: Optional[str] = Cookie(None)
):
    if not user:
        return RedirectResponse(url="/")

    df = read_contracts()
    df = df[df["ContractID"] != contract_id]
    save_contracts(df)
    delete_attachment(contract_id)
    return RedirectResponse(
        url=router.url_path_for("manage_contracts_page"), status_code=303
    )


@router.get("/contract_files/{file_path:path}")
async def serve_contract_file(file_path: str, user: Optional[str] = Cookie(None)):
    if not user:
        return RedirectResponse(url="/")

    base_dir = os.path.abspath(CONTRACT_ATTACHMENT_DIR)
    requested_path = os.path.join(base_dir, file_path)
    if not os.path.abspath(requested_path).startswith(base_dir):
        return HTMLResponse(content="Forbidden", status_code=403)
    if os.path.exists(requested_path) and os.path.isfile(requested_path):
        return FileResponse(requested_path)
    return HTMLResponse(content="File Not Found", status_code=404)
