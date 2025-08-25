# add_employees.py
import sqlite3
import os
from pathlib import Path

# --- 重要 ---
# 此腳本應放在您的專案根目錄下。
# 它假設資料庫位於相對於根目錄的 'data/approvals.db'。
# -----------------

# 定義資料庫路徑，與 approvals_router.py 保持一致
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "approvals.db"


def get_conn():
    """建立與 SQLite 資料庫的連線。"""
    # 確保 'data' 資料夾存在
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB_PATH)


def setup_database():
    """
    確保 employees 資料表存在，並檢查是否需要新增範例資料。
    """
    with get_conn() as conn:
        c = conn.cursor()

        # 建立資料表 (如果不存在)
        c.execute(
            """
        CREATE TABLE IF NOT EXISTS employees(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder TEXT,
            cost_center TEXT,
            department TEXT,
            department_1 TEXT,
            team TEXT,
            job_title TEXT,
            name TEXT,
            employee_id TEXT,
            english_name TEXT,
            email TEXT,
            extension_number TEXT
        )
        """
        )
        print("✔️ 'employees' 資料表已確認存在。")

        # 檢查資料表是否已經有資料
        c.execute("SELECT COUNT(*) FROM employees")
        count = c.fetchone()[0]
        if count > 0:
            print(f"ℹ️ 'employees' 資料表已有 {count} 筆記錄，將不新增範例資料。")
            return False  # 不需要新增資料

        conn.commit()
        return True  # 可以新增資料


def add_sample_data():
    """
    在 employees 資料表中插入範例員工資料。
    """
    # 範例資料: (department, department_1, name, english_name, email)
    sample_employees = [
        ("工程部", "硬體組", "王大明", "David Wang", "david.wang@example.com"),
        ("工程部", "韌體組", "李小美", "Amy Lee", "amy.lee@example.com"),
        ("工程部", "軟體組", "陳志豪", "John Chen", "john.chen@example.com"),
        ("業務部", None, "林秀芬", "Sophia Lin", "sophia.lin@example.com"),
        ("業務部", "國內業務", "張偉誠", "Wilson Chang", "wilson.chang@example.com"),
        ("管理部", "人資組", "黃心怡", "Cindy Huang", "cindy.huang@example.com"),
        ("管理部", "財務組", "劉俊傑", "Jack Liu", "jack.liu@example.com"),
    ]

    with get_conn() as conn:
        c = conn.cursor()
        c.executemany(
            """
            INSERT INTO employees (department, department_1, name, english_name, email)
            VALUES (?, ?, ?, ?, ?)
            """,
            sample_employees,
        )
        conn.commit()
        print(f"✅ 成功新增 {len(sample_employees)} 筆範例員工資料。")


if __name__ == "__main__":
    print(f"正在設定資料庫: {DB_PATH}")
    if setup_database():
        add_sample_data()
    print("腳本執行完畢。")
