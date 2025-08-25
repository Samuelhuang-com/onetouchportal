# data/migrate_permissions_from_excel.py
import os, sys, json
import pandas as pd

# --- 確保能匯入專案根目錄的模組（例如 app_utils）---
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# ---------------------------------------------------

from app_utils import PERMISSION_FILE, NAV_ITEMS  # 權限 Excel 與導覽列
from data.db import init_db, get_conn, upsert_permission_keys

def flatten_permission_keys(items):
    keys = set()
    for it in items:
        k = it.get("permission_key")
        if k: keys.add(k)
        for sub in it.get("sub_items", []):
            sk = sub.get("permission_key")
            if sk: keys.add(sk)
    return sorted(keys)

def run():
    # 1) 建表
    init_db()

    # 2) 同步權限鍵（以 NAV_ITEMS 為準）
    perm_keys = flatten_permission_keys(NAV_ITEMS)
    upsert_permission_keys(perm_keys)

    # 3) 讀 Excel（全欄位字串、空值 -> ""）
    df = pd.read_excel(PERMISSION_FILE, sheet_name="employeesfiles", dtype=str).fillna("")

    with get_conn() as c:
        for _, r in df.iterrows():
            login = r.get("loginname","").strip()
            pwd   = r.get("password","").strip()
            role  = (r.get("role","guest") or "guest").strip()
            name  = (r.get("name","") or r.get("display_name","")).strip()
            status_str = str(r.get("status","")).strip().upper()
            status = 1 if status_str in ("TRUE","1","Y","YES","ON") else 0

            if not login or not pwd:
                continue  # 跳過無效列

            # upsert 到 users
            c.execute("""
                INSERT INTO users(loginname, password, display_name, role, status)
                VALUES (?,?,?,?,?)
                ON CONFLICT(loginname) DO UPDATE SET
                  password=excluded.password,
                  display_name=excluded.display_name,
                  role=excluded.role,
                  status=excluded.status,
                  updated_at=CURRENT_TIMESTAMP
            """, (login, pwd, name, role, status))

            uid = c.execute("SELECT id FROM users WHERE loginname=?", (login,)).fetchone()["id"]

            # 逐一權限鍵寫入 user_permissions
            for k in perm_keys:
                v = 1 if str(r.get(k,"")).strip().upper() in ("TRUE","1","Y","YES","ON") else 0
                c.execute("""
                    INSERT INTO user_permissions(user_id, perm_key, value)
                    VALUES (?,?,?)
                    ON CONFLICT(user_id,perm_key) DO UPDATE SET value=excluded.value
                """, (uid, k, v))

if __name__ == "__main__":
    run()
